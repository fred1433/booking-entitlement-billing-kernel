"""The monthly run.

The run has one job and one promise. The job: produce exactly one line per
entitlement per period. The promise: never take money twice for the same line,
whatever happens in the middle.

Three things stand between the promise and reality, and only one of them is
code the kernel controls:

* a unique index on (entitlement, period), so a second line cannot exist even
  if the run is started twice by two schedulers;
* a derived idempotency key, so a charge replayed after a lost response reaches
  the provider as the same request rather than a new one;
* a commit per line, so a crash loses at most the record of one charge, and the
  key above makes recovering that one charge free.

The run is therefore safe to start again at any moment, including while it is
already running, which is the only property that makes a billing job something
you can operate at three in the morning.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from .. import journal
from ..clock import month_bounds, utc_now
from ..entitlements.state import BILLABLE
from ..models import (
    Booking,
    BillingLine,
    BillingRun,
    Entitlement,
    LineState,
    QuarantineItem,
)
from .payments import IdempotencyConflict, PaymentProvider, ProviderUnavailable


@dataclass
class BillingRunResult:
    run_id: int
    period: str
    prepared: int = 0
    already_prepared: int = 0
    blocked: int = 0
    charged: int = 0
    replayed: int = 0
    failed: int = 0
    lines: list[int] = field(default_factory=list)


def idempotency_key(entitlement_id: int, period: str) -> str:
    """Derived, never random. The same line always presents the same key."""
    return f"bill:{entitlement_id}:{period}"


def prepare(session: Session, period: str, run: BillingRun | None = None) -> BillingRunResult:
    """Write the lines for a period. Safe to call as often as you like."""
    run = run or _open_run(session, period)
    result = BillingRunResult(run_id=run.id, period=period)
    period_start, period_end = month_bounds(period)

    candidates = session.execute(
        select(Entitlement, Booking)
        .join(Booking, Booking.id == Entitlement.booking_id)
        .where(
            Entitlement.state.in_(tuple(BILLABLE)),
            Entitlement.billable_from < period_end,
            Entitlement.billable_to >= period_start,
        )
        .order_by(Entitlement.id)
    ).all()

    for entitlement, booking in candidates:
        blocked_by = _open_quarantine(session, booking)
        amount = booking.unit_amount_cents * booking.quantity
        state = LineState.BLOCKED.value if blocked_by else LineState.PREPARED.value
        reason = (
            f"source data is in quarantine: {blocked_by.kind}" if blocked_by else None
        )

        line_id = session.execute(
            pg_insert(BillingLine)
            .values(
                run_id=run.id,
                entitlement_id=entitlement.id,
                period=period,
                amount_cents=amount,
                currency=booking.currency,
                idempotency_key=idempotency_key(entitlement.id, period),
                state=state,
                reason=reason,
            )
            .on_conflict_do_nothing(constraint="uq_line_per_entitlement_period")
            .returning(BillingLine.id)
        ).scalar_one_or_none()

        if line_id is None:
            result.already_prepared += 1
            continue

        result.lines.append(line_id)
        if blocked_by:
            result.blocked += 1
            journal.refused(
                session, "billing.prepare", f"entitlement/{entitlement.id}",
                reason, {"period": period, "line": line_id},
            )
        else:
            result.prepared += 1

    session.flush()
    journal.accepted(
        session, "billing.prepare", f"period/{period}", None,
        {"prepared": result.prepared, "already_prepared": result.already_prepared,
         "blocked": result.blocked},
    )
    session.commit()
    return result


def charge_prepared(
    session: Session,
    period: str,
    provider: PaymentProvider,
    result: BillingRunResult | None = None,
    after_each: Callable[[int], None] | None = None,
) -> BillingRunResult:
    """Charge every prepared line of a period, one commit at a time.

    ``after_each`` runs after a line is charged and committed. The tests use it
    to stop the run at an awkward moment, which is the only moment worth
    testing.
    """
    result = result or BillingRunResult(run_id=0, period=period)
    lines = session.execute(
        select(BillingLine)
        .where(BillingLine.period == period, BillingLine.state == LineState.PREPARED.value)
        .order_by(BillingLine.id)
    ).scalars().all()

    for line in lines:
        try:
            charge = provider.create_charge(
                idempotency_key=line.idempotency_key,
                amount_cents=line.amount_cents,
                currency=line.currency,
                metadata={"entitlement": str(line.entitlement_id), "period": period},
            )
        except ProviderUnavailable as error:
            # The money may or may not have moved. The line stays prepared, and
            # the next run presents the same key: the provider replays its own
            # answer instead of charging again.
            result.failed += 1
            journal.refused(
                session, "billing.charge", f"line/{line.id}",
                f"provider did not answer: {error}",
                {"idempotency_key": line.idempotency_key, "recovery": "same key on the next run"},
            )
            session.commit()
            continue
        except IdempotencyConflict as error:
            result.failed += 1
            line.state = LineState.BLOCKED.value
            line.reason = str(error)
            journal.refused(
                session, "billing.charge", f"line/{line.id}",
                f"idempotency key reused for a different request: {error}",
                {"idempotency_key": line.idempotency_key},
            )
            session.commit()
            continue

        line.state = LineState.CHARGED.value
        line.charge_ref = charge.id
        line.charged_at = utc_now()
        if charge.replayed:
            result.replayed += 1
            journal.accepted(
                session, "billing.charge", f"line/{line.id}",
                "provider replayed the charge it had already taken for this key",
                {"charge": charge.id, "idempotency_key": line.idempotency_key},
            )
        else:
            result.charged += 1
            journal.accepted(session, "billing.charge", f"line/{line.id}", None,
                             {"charge": charge.id, "amount_cents": line.amount_cents})
        session.commit()
        if after_each is not None:
            after_each(line.id)

    return result


def run_billing(
    session: Session,
    period: str,
    provider: PaymentProvider,
    after_each: Callable[[int], None] | None = None,
) -> BillingRunResult:
    run = _open_run(session, period)
    result = prepare(session, period, run)
    charge_prepared(session, period, provider, result, after_each=after_each)
    run.finished_at = utc_now()
    run.state = "completed"
    session.commit()
    return result


def on_entitlement_cancelled(session: Session, entitlement: Entitlement) -> None:
    """A cancellation never rewrites history, it states what is owed.

    A line that was prepared and not yet charged is voided. A line that was
    already charged becomes a refund that somebody has to decide about, and it
    stays on the reconciliation until they do. Deleting it would make the next
    run bill the period again.
    """
    lines = session.execute(
        select(BillingLine).where(BillingLine.entitlement_id == entitlement.id)
    ).scalars().all()

    for line in lines:
        if line.state == LineState.PREPARED.value or line.state == LineState.BLOCKED.value:
            line.state = LineState.VOIDED.value
            line.reason = "entitlement cancelled before the charge was taken"
            journal.accepted(session, "billing.void", f"line/{line.id}", line.reason,
                             {"period": line.period})
        elif line.state == LineState.CHARGED.value:
            line.state = LineState.REFUND_DUE.value
            line.reason = "entitlement cancelled after the charge was taken"
            journal.refused(
                session, "billing.cancel", f"line/{line.id}",
                "cancelled after the charge, a refund is owed and is not automatic",
                {"period": line.period, "charge": line.charge_ref,
                 "amount_cents": line.amount_cents},
            )
    session.flush()


def unblock_line(session: Session, line_id: int, note: str) -> BillingLine:
    """Release a line once the partner has answered the question that blocked it."""
    line = session.get(BillingLine, line_id)
    if line.state != LineState.BLOCKED.value:
        raise RuntimeError(f"line {line_id} is {line.state}, not blocked")
    line.state = LineState.PREPARED.value
    line.reason = f"unblocked: {note}"
    journal.accepted(session, "billing.unblock", f"line/{line.id}", note, {"period": line.period})
    session.flush()
    return line


def _open_run(session: Session, period: str) -> BillingRun:
    attempt = session.execute(
        select(BillingRun).where(BillingRun.period == period)
    ).scalars().all()
    run = BillingRun(period=period, state="running", attempt=len(attempt) + 1)
    session.add(run)
    session.flush()
    return run


def _open_quarantine(session: Session, booking: Booking) -> QuarantineItem | None:
    return session.execute(
        select(QuarantineItem).where(
            QuarantineItem.partner == booking.partner,
            QuarantineItem.external_id == booking.external_id,
            QuarantineItem.resolved_at.is_(None),
        ).limit(1)
    ).scalar_one_or_none()
