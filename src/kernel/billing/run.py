"""Preparation, submission, and what a resume is allowed to do.

The order of operations is the whole subject:

1. **prepare** writes one line per (entitlement, period). It is an insert that
   does nothing on conflict, so two workers preparing the same period at the
   same moment produce one line, not two.
2. **submit** claims a prepared line with a conditional update, commits that
   claim, and only then calls the external system. The committed claim is the
   durable intent: it carries the derived key and the frozen parameters, and it
   is what makes a safe resume possible at all.
3. **resume** looks at intents whose outcome is unknown. Inside the retention
   window it presents the same key with the same parameters, which replays
   rather than repeats. Outside it, it refuses, because presenting a forgotten
   key would create a new request. Those lines are marked unresolved and wait
   for an outcome established another way.

What this establishes is bounded, and worth stating plainly: within this model,
with this simulated external system, a lost response does not become a second
operation. It does not validate an integration with a real provider, which this
sample does not exercise.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, sessionmaker

from .. import journal, mutations
from ..clock import month_bounds, utc_now
from ..models import (
    BillingLine,
    BillingRun,
    Booking,
    Cancellation,
    Entitlement,
    EntitlementState,
    LineState,
    QuarantineItem,
)
from .payments import (
    KEY_RETENTION,
    IdempotencyConflict,
    KeyNoLongerRetained,
    PaymentProvider,
    ProviderUnavailable,
    request_fingerprint,
)


def billable_amount(booking: Booking) -> int:
    """What this booking is worth for a period, according to a declaration.

    The multiplication is not arithmetic, it is an interpretation of a sentence
    somebody said about a field. One feed here means the price of a unit and the
    other means the total for the row, so the identical number 18000 with
    quantity 3 is either 540.00 or 180.00, and both readings parse.

    This is where normalisation has to stop. The shape can be made uniform; the
    meaning cannot be recovered from the payload, and getting it wrong is
    invisible until an invoice is disputed.
    """
    if booking.amount_basis == "per_booking":
        return booking.amount_cents
    return booking.amount_cents * booking.quantity


def idempotency_key_for(entitlement_id: int, period: str) -> str:
    """Derived from the line, never generated per attempt.

    The one exception is a deliberate mutation used by the suite to show what
    a per-attempt key costs.
    """
    if mutations.is_disabled(mutations.DERIVED_IDEMPOTENCY_KEY):
        return f"bill:{entitlement_id}:{period}:{uuid.uuid4().hex[:8]}"
    return f"bill:{entitlement_id}:{period}"


# --------------------------------------------------------------------------- #
# 1. Preparation
# --------------------------------------------------------------------------- #

def _is_undecidable(session: Session, booking: Booking) -> str | None:
    """Whether anything about this booking is still an open question.

    A booking whose latest delivery could not be decided, or which has an open
    quarantine item, is not billed. This is a conservative demonstration
    policy, not a rule anybody has agreed to: a real product might well choose
    to bill the last confirmed state and correct later. The point is that the
    choice is explicit and visible, rather than an accident of ordering.
    """
    if mutations.is_disabled(mutations.UNDECIDED_BLOCKS_BILLING):
        return None
    if booking.undecided_reason:
        return booking.undecided_reason
    open_item = session.scalar(
        select(QuarantineItem).where(
            QuarantineItem.partner == booking.partner,
            QuarantineItem.external_id == booking.external_id,
            QuarantineItem.resolved_at.is_(None),
        )
    )
    return open_item.kind if open_item is not None else None


def prepare(session: Session, period: str, now: datetime | None = None) -> BillingRun:
    """Write one line per billable entitlement for the period.

    Running it twice produces the same lines. Running it twice at the same
    moment on two workers also produces the same lines, because the second
    insert conflicts with the first on (entitlement, period) and does nothing.
    """
    now = now or utc_now()
    start, end = month_bounds(period)

    run = BillingRun(period=period, state="running", attempt=1)
    session.add(run)
    session.flush()

    rows = session.execute(
        select(Entitlement, Booking)
        .join(Booking, Booking.id == Entitlement.booking_id)
        .where(Entitlement.billable_from < end)
        .where((Entitlement.billable_to.is_(None)) | (Entitlement.billable_to > start))
    ).all()

    for entitlement, booking in rows:
        blocked_reason = _is_undecidable(session, booking)
        amount = billable_amount(booking)
        key = idempotency_key_for(entitlement.id, period)

        if blocked_reason:
            state, reason = LineState.BLOCKED.value, blocked_reason
        elif entitlement.state == EntitlementState.CANCELLED.value:
            state, reason = LineState.VOIDED.value, "cancelled before the period was prepared"
        else:
            state, reason = LineState.PREPARED.value, None

        statement = (
            pg_insert(BillingLine)
            .values(
                run_id=run.id,
                entitlement_id=entitlement.id,
                period=period,
                amount_cents=amount,
                currency=booking.currency,
                idempotency_key=key,
                request_fingerprint=request_fingerprint(amount, booking.currency),
                state=state,
                reason=reason,
                cancellation=Cancellation.NONE.value,
                prepared_at=now,
            )
            .on_conflict_do_nothing(constraint="uq_line_per_entitlement_period")
            .returning(BillingLine.id)
        )
        created = session.execute(statement).scalar_one_or_none()

        if created is None:
            journal.accepted(
                session, "billing.prepare", f"entitlement/{entitlement.id}",
                "a line for this entitlement and period already exists",
            )
        elif blocked_reason:
            journal.refused(
                session, "billing.prepare", f"entitlement/{entitlement.id}",
                f"not prepared while a question is open: {blocked_reason}",
            )
        else:
            journal.accepted(session, "billing.prepare", f"entitlement/{entitlement.id}")

    run.finished_at = now
    run.state = "prepared"
    session.flush()
    return run


# --------------------------------------------------------------------------- #
# 2. Submission
# --------------------------------------------------------------------------- #

def _claim(session: Session, line_id: int, now: datetime) -> bool:
    """Take a prepared line, atomically.

    One statement moves the line out of ``prepared``, so a second worker
    reading the same line a microsecond later finds nothing to take. A read
    followed by a write would let both of them through.
    """
    claimed = session.execute(
        text(
            "update billing_lines set state = :submitted, submitted_at = :now "
            "where id = :id and state = :prepared returning id"
        ),
        {
            "submitted": LineState.SUBMITTED.value,
            "prepared": LineState.PREPARED.value,
            "now": now,
            "id": line_id,
        },
    ).scalar_one_or_none()
    return claimed is not None


def submit(
    session_factory: sessionmaker[Session],
    provider: PaymentProvider,
    period: str,
    now: datetime | None = None,
) -> dict[str, int]:
    """Submit every prepared line for the period, one committed step at a time.

    Each line gets its own transaction, because a worker that dies halfway
    through a period must leave the lines it finished finished, and the line it
    was on marked as attempted.
    """
    now = now or utc_now()
    summary = {"submitted": 0, "charged": 0, "lost": 0, "refused": 0}

    with session_factory() as reader:
        line_ids = list(
            reader.scalars(
                select(BillingLine.id)
                .where(BillingLine.period == period)
                .where(BillingLine.state == LineState.PREPARED.value)
                .order_by(BillingLine.id)
            )
        )

    for line_id in line_ids:
        with session_factory() as session:
            if not _claim(session, line_id, now):
                session.commit()
                continue
            line = session.get(BillingLine, line_id)
            key, fingerprint = line.idempotency_key, line.request_fingerprint
            amount, currency = line.amount_cents, line.currency
            if mutations.is_disabled(mutations.INTENT_BEFORE_CALL):
                session.rollback()          # the evidence is thrown away
            else:
                session.commit()            # the intent is durable from here on
            summary["submitted"] += 1

        try:
            result = provider.create_charge(key, fingerprint, amount, currency)
        except ProviderUnavailable as lost:
            summary["lost"] += 1
            with session_factory() as session:
                journal.refused(session, "billing.submit", f"line/{line_id}", str(lost))
                session.commit()
            continue
        except IdempotencyConflict as clash:
            summary["refused"] += 1
            with session_factory() as session:
                journal.refused(session, "billing.submit", f"line/{line_id}", str(clash))
                session.commit()
            continue

        with session_factory() as session:
            _settle(session, line_id, result.charge_ref, now)
            journal.accepted(
                session, "billing.submit", f"line/{line_id}",
                "replayed by the provider" if result.replayed else None,
            )
            session.commit()
        summary["charged"] += 1

    return summary


def _settle(session: Session, line_id: int, charge_ref: str, now: datetime) -> None:
    line = session.get(BillingLine, line_id)
    line.state = LineState.CHARGED.value
    line.charge_ref = charge_ref
    line.resolved_at = now


# --------------------------------------------------------------------------- #
# 3. Resume
# --------------------------------------------------------------------------- #

def resume(
    session_factory: sessionmaker[Session],
    provider: PaymentProvider,
    period: str,
    now: datetime | None = None,
    retention: timedelta = KEY_RETENTION,
) -> dict[str, int]:
    """Deal with intents whose outcome nobody knows.

    Inside the retention window the same key with the same parameters is
    presented again: that replays the stored result rather than repeating the
    operation, and the line is settled on the answer that comes back.

    Outside the window the key has been forgotten by the other side. Sending it
    again would create a new request, so nothing is sent. The line becomes
    unresolved and stays on the report until the outcome is established another
    way. This is the branch that usually does not exist, and it is the reason
    the window is a parameter rather than a constant in a comment.
    """
    now = now or utc_now()
    summary = {"replayed": 0, "unresolved": 0, "still_lost": 0}

    with session_factory() as reader:
        pending = list(
            reader.execute(
                select(BillingLine.id, BillingLine.submitted_at)
                .where(BillingLine.period == period)
                .where(BillingLine.state == LineState.SUBMITTED.value)
                .order_by(BillingLine.id)
            ).all()
        )

    for line_id, submitted_at in pending:
        age = now - submitted_at if submitted_at else timedelta(0)
        if age > retention:
            with session_factory() as session:
                line = session.get(BillingLine, line_id)
                line.state = LineState.UNRESOLVED.value
                line.reason = (
                    "outcome unknown and the key is past the provider retention window, "
                    "so it must not be presented again: establish the result out of band"
                )
                journal.refused(session, "billing.resume", f"line/{line_id}", line.reason)
                session.commit()
            summary["unresolved"] += 1
            continue

        with session_factory() as reader:
            line = reader.get(BillingLine, line_id)
            key, fingerprint = line.idempotency_key, line.request_fingerprint
            amount, currency = line.amount_cents, line.currency

        try:
            result = provider.create_charge(key, fingerprint, amount, currency)
        except (ProviderUnavailable, KeyNoLongerRetained) as still_unknown:
            summary["still_lost"] += 1
            with session_factory() as session:
                journal.refused(session, "billing.resume", f"line/{line_id}", str(still_unknown))
                session.commit()
            continue

        with session_factory() as session:
            _settle(session, line_id, result.charge_ref, now)
            journal.accepted(
                session, "billing.resume", f"line/{line_id}",
                "the provider replayed the operation it had already carried out"
                if result.replayed else "carried out on this attempt",
            )
            session.commit()
        summary["replayed"] += 1

    return summary


def establish_out_of_band(
    session: Session,
    provider: PaymentProvider,
    line_id: int,
    now: datetime | None = None,
) -> str:
    """Close an unresolved line by looking the outcome up rather than retrying.

    This is the manual step a person performs against the provider's own
    records. It is in the repository because the alternative, quietly retrying,
    is the thing that turns one operation into two.
    """
    now = now or utc_now()
    line = session.get(BillingLine, line_id)
    found = provider.lookup(line.idempotency_key)
    if found is None:
        line.state = LineState.PREPARED.value
        line.submitted_at = None
        line.reason = "established out of band: the operation never happened, safe to prepare again"
        journal.accepted(session, "billing.establish", f"line/{line_id}", line.reason)
        return "never_happened"
    line.state = LineState.CHARGED.value
    line.charge_ref = found.charge_ref
    line.resolved_at = now
    line.reason = "established out of band: the operation had happened, no second attempt"
    journal.accepted(session, "billing.establish", f"line/{line_id}", line.reason)
    return "already_happened"


# --------------------------------------------------------------------------- #
# 4. Cancellation, in the three places it can land
# --------------------------------------------------------------------------- #

def cancel(session: Session, entitlement_id: int, reason: str, now: datetime | None = None) -> dict[str, str]:
    """Record a cancellation against whatever state each line is in.

    Three situations, and they are not the same situation:

    * before anything was submitted, the line is voided and that is the end of it;
    * while the outcome is unknown, nothing can be decided at all, because
      deciding requires knowing whether money moved;
    * after the operation is established, the line stays. What is owed to whom
      is a commercial question, and no rule for it is invented here.

    No line is ever deleted. Deleting is what makes the next run find nothing
    for the period and do the whole thing again.
    """
    now = now or utc_now()
    entitlement = session.get(Entitlement, entitlement_id)
    entitlement.state = EntitlementState.CANCELLED.value
    entitlement.cancelled_at = now
    entitlement.cancel_reason = reason

    outcomes: dict[str, str] = {}
    lines = session.scalars(
        select(BillingLine).where(BillingLine.entitlement_id == entitlement_id)
    ).all()

    for line in lines:
        if line.state in (LineState.PREPARED.value, LineState.BLOCKED.value):
            line.state = LineState.VOIDED.value
            line.cancellation = Cancellation.BEFORE_SUBMISSION.value
            line.reason = "cancelled before anything was submitted"
            journal.accepted(session, "billing.cancel", f"line/{line.id}", line.reason)
            outcomes[str(line.id)] = Cancellation.BEFORE_SUBMISSION.value

        elif line.state in (LineState.SUBMITTED.value, LineState.UNRESOLVED.value):
            line.cancellation = Cancellation.WHILE_UNRESOLVED.value
            line.reason = (
                "cancelled while the outcome was unknown: nothing can be decided "
                "until it is established whether the operation happened"
            )
            journal.refused(session, "billing.cancel", f"line/{line.id}", line.reason)
            outcomes[str(line.id)] = Cancellation.WHILE_UNRESOLVED.value

        elif line.state == LineState.CHARGED.value:
            line.cancellation = Cancellation.AFTER_CHARGE.value
            line.reason = (
                "cancelled after the operation was established: what is owed is a "
                "commercial decision, and this sample does not make it"
            )
            journal.refused(session, "billing.cancel", f"line/{line.id}", line.reason)
            outcomes[str(line.id)] = Cancellation.AFTER_CHARGE.value

    session.flush()
    return outcomes
