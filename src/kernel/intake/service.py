"""Intake: one message in, one recorded decision out.

The rules, in the order they are applied:

1. The adapter translates, or refuses. A refusal is quarantined with the
   sentence to send to the partner. It is never defaulted.
2. If the partner gives its events an id, a unique index makes the second
   arrival of that id a no-op. Two workers racing on the same retry cannot both
   win, because the race is settled by the database.
3. A booking is identified by (partner, external_id) and nothing else.
4. An update is applied only if it is provably newer than what we hold.
   Provably older is refused. Equal with the same content is a duplicate.
   Equal with different content is a conflict and is not resolved by the
   kernel. Not provable, which is what a naive local timestamp gives you in the
   repeated hour, is refused and raised with the partner.

The one rule underneath all of them: the kernel never invents a fact about a
booking in order to keep moving.
"""

from __future__ import annotations

import csv
import io
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from .. import journal, mutations
from ..clock import Instant, Order, compare, utc_now
from ..models import (
    BatchState,
    Booking,
    Delivery,
    DeliveryOutcome,
    ImportBatch,
    ProcessedEvent,
    QuarantineItem,
    VersionConflict,
)
from .schemas import CanonicalBooking, Rejection, canonical_fingerprint

if TYPE_CHECKING:
    from ..partners.base import PartnerAdapter


@dataclass
class IntakeResult:
    outcome: DeliveryOutcome
    delivery_id: int
    booking_id: int | None = None
    reason: str | None = None

    @property
    def applied(self) -> bool:
        return self.outcome is DeliveryOutcome.APPLIED


@dataclass
class ImportResult:
    batch_id: str
    state: BatchState
    reason: str | None
    rows: int
    applied: int
    quarantined: int
    results: list[IntakeResult]


def apply_delivery(
    session: Session,
    adapter: "PartnerAdapter",
    raw: dict,
    channel: str = "push",
    batch_id: str | None = None,
) -> IntakeResult:
    """Record one inbound message and decide what it is allowed to change."""
    normalised = adapter.normalise(raw)

    if isinstance(normalised, Rejection):
        return _quarantine(session, adapter, raw, channel, batch_id, normalised)

    delivery = Delivery(
        partner=normalised.partner,
        channel=channel,
        source_event_id=normalised.source_event_id,
        external_id=normalised.external_id,
        payload=raw,
        payload_fingerprint=canonical_fingerprint(normalised),
        updated_at_raw=normalised.updated_at.raw,
        updated_at_lo=normalised.updated_at.lo,
        updated_at_hi=normalised.updated_at.hi,
        updated_at_ambiguous=normalised.updated_at.ambiguous,
        batch_id=batch_id,
        outcome=DeliveryOutcome.APPLIED.value,
    )
    session.add(delivery)
    session.flush()

    subject = f"{normalised.partner}/{normalised.external_id}"

    if normalised.source_event_id:
        claimed = session.execute(
            pg_insert(ProcessedEvent)
            .values(
                partner=normalised.partner,
                source_event_id=normalised.source_event_id,
                first_delivery_id=delivery.id,
            )
            .on_conflict_do_nothing(constraint="uq_processed_event")
            .returning(ProcessedEvent.id)
        ).scalar_one_or_none()
        if claimed is None:
            return _finish(
                session, delivery, DeliveryOutcome.DUPLICATE,
                reason="event id already processed",
                subject=subject, refused=False,
                detail={"event_id": normalised.source_event_id},
            )

    stored = session.execute(
        select(Booking)
        .where(Booking.partner == normalised.partner, Booking.external_id == normalised.external_id)
        .with_for_update()
    ).scalar_one_or_none()

    if stored is None:
        created_id = session.execute(
            pg_insert(Booking)
            .values(**_booking_values(normalised, delivery.id))
            .on_conflict_do_nothing(constraint="uq_booking_identity")
            .returning(Booking.id)
        ).scalar_one_or_none()
        if created_id is not None:
            delivery.booking_id = created_id
            session.flush()
            # The decision is journalled before its consequences, so the journal
            # reads in the order things happened rather than in the order the
            # call stack unwound.
            result = _finish(session, delivery, DeliveryOutcome.APPLIED,
                             reason="first delivery for this booking",
                             subject=subject, refused=False)
            _drive_entitlement(session, created_id)
            return result
        # Another worker inserted it between the select and the insert.
        stored = session.execute(
            select(Booking)
            .where(Booking.partner == normalised.partner,
                   Booking.external_id == normalised.external_id)
            .with_for_update()
        ).scalar_one()

    delivery.booking_id = stored.id
    held = Instant(
        raw=stored.updated_at_raw,
        lo=stored.updated_at_lo,
        hi=stored.updated_at_hi,
        ambiguous=stored.updated_at_ambiguous,
    )
    order = compare(normalised.updated_at, held)

    if order is Order.OLDER:
        return _finish(
            session, delivery, DeliveryOutcome.SUPERSEDED,
            reason="older than the state we hold, applying it would move the booking backwards",
            subject=subject, refused=True,
            detail={"delivered": normalised.updated_at.raw, "held": stored.updated_at_raw},
        )

    if order is Order.NOT_PROVABLE:
        _raise_quarantine(
            session, normalised.partner, normalised.external_id, delivery.id,
            kind="order_not_provable",
            detail=(
                f"delivered {normalised.updated_at.raw!r} cannot be ordered against held "
                f"{stored.updated_at_raw!r}: at least one of them is a local wall clock "
                "reading that happened twice"
            ),
            needed_from_partner=(
                "Send changed_at in UTC or with an explicit offset. One hour a year, a "
                "naive local time is two instants, and nothing in the payload says which."
            ),
        )
        stored.undecided_reason = "order_not_provable"
        session.flush()
        return _finish(
            session, delivery, DeliveryOutcome.ORDER_NOT_PROVABLE,
            reason="order against the held state cannot be proved",
            subject=subject, refused=True,
            detail={"delivered": normalised.updated_at.raw, "held": stored.updated_at_raw},
        )

    fingerprint = canonical_fingerprint(normalised)

    if order is Order.SAME:
        if fingerprint == stored.payload_fingerprint:
            return _finish(
                session, delivery, DeliveryOutcome.DUPLICATE,
                reason="same timestamp, same content",
                subject=subject, refused=False,
            )
        if mutations.is_disabled(mutations.CONFLICT_IS_NOT_A_DUPLICATE):
            # The mutation: treat it as the latest arrival and move on. Nobody
            # is told, and one of the two versions is gone.
            for field, value in _booking_values(normalised, delivery.id).items():
                if field in ("partner", "external_id", "first_seen_at"):
                    continue
                setattr(stored, field, value)
            session.flush()
            return _finish(
                session, delivery, DeliveryOutcome.APPLIED,
                reason=None, subject=subject, refused=False,
            )

        question = (
            "These two records have the same booking ID and updatedAt but different "
            "statuses. Can updatedAt identify a revision, or is there a separate "
            "sequence? The affected booking remains on billing hold until this is "
            "resolved."
        )
        session.add(
            VersionConflict(
                booking_id=stored.id,
                delivery_id=delivery.id,
                held_fingerprint=stored.payload_fingerprint,
                incoming_fingerprint=fingerprint,
                held_payload={"status": stored.status, "quantity": stored.quantity,
                              "updated_at": stored.updated_at_raw},
                incoming_payload=raw,
                question_for_partner=question,
            )
        )
        _raise_quarantine(
            session, normalised.partner, normalised.external_id, delivery.id,
            kind="conflicting_versions",
            detail=(
                f"two different versions of this booking carry the same timestamp "
                f"{normalised.updated_at.raw!r}"
            ),
            needed_from_partner=question,
        )
        # Both versions are kept and neither is applied. Until somebody answers,
        # nothing about this booking is decidable, so nothing about it is billed.
        stored.undecided_reason = "conflicting_versions"
        session.flush()
        return _finish(
            session, delivery, DeliveryOutcome.CONFLICT,
            reason="same identity, same version, different content",
            subject=subject, refused=True,
            detail={"held_fingerprint": stored.payload_fingerprint, "delivered_fingerprint": fingerprint},
        )

    # Provably newer.
    unchanged = fingerprint == stored.payload_fingerprint
    for field, value in _booking_values(normalised, delivery.id).items():
        if field in ("partner", "external_id", "first_seen_at"):
            continue
        setattr(stored, field, value)
    stored.last_applied_at = utc_now()
    session.flush()
    result = _finish(
        session, delivery, DeliveryOutcome.APPLIED,
        reason="no business change, only a newer timestamp" if unchanged else None,
        subject=subject, refused=False,
    )
    _drive_entitlement(session, stored.id)
    return result


def run_pull_window(
    session: Session,
    adapter: "PartnerAdapter",
    items: list[dict],
) -> list[IntakeResult]:
    """Apply one pull window.

    Pull windows overlap on purpose, because a window that does not overlap
    loses whatever was written during the gap. Overlap is only safe if
    reprocessing is free, which is the whole point of the rules above.
    """
    return [apply_delivery(session, adapter, item, channel="pull") for item in items]


def import_file(
    session: Session,
    adapter: "PartnerAdapter",
    filename: str,
    content: str,
    min_expected_rows: int = 0,
) -> ImportResult:
    """Apply one partner file, as a whole or not at all.

    Structural problems stop the batch: a missing column, a truncated download,
    a declared row count that does not match, a file far shorter than this
    feed's own history. Row level problems do not: one unreadable row is
    quarantined and the other four thousand are applied, because holding a good
    night hostage to one bad row is its own outage.
    """
    batch_id = uuid.uuid4().hex[:16]
    trailer_rows, body = _split_trailer(content)
    reader = csv.reader(io.StringIO(body))

    try:
        header = next(reader)
    except StopIteration:
        return _reject_batch(session, adapter, batch_id, filename, 0,
                             "the file has no header row")

    mapping, unresolved = adapter.resolve_columns(header)
    if unresolved:
        return _reject_batch(
            session, adapter, batch_id, filename, 0,
            f"unknown file shape, no declared spelling found for: {', '.join(unresolved)}",
        )

    raw_rows = [row for row in reader if row]
    for index, row in enumerate(raw_rows, start=2):
        if len(row) != len(header):
            return _reject_batch(
                session, adapter, batch_id, filename, len(raw_rows),
                f"truncated file, line {index} has {len(row)} fields and the header has {len(header)}",
            )

    if not body.endswith("\n"):
        return _reject_batch(
            session, adapter, batch_id, filename, len(raw_rows),
            "truncated file, the last line has no terminator",
        )

    if trailer_rows is not None and trailer_rows != len(raw_rows):
        return _reject_batch(
            session, adapter, batch_id, filename, len(raw_rows),
            f"the file declares {trailer_rows} rows and carries {len(raw_rows)}",
        )

    if min_expected_rows and len(raw_rows) < min_expected_rows:
        batch = ImportBatch(
            partner=adapter.name, batch_id=batch_id, filename=filename,
            row_count=len(raw_rows), applied_rows=0, quarantined_rows=0,
            state=BatchState.HELD_FOR_REVIEW.value,
            reason=(
                f"{len(raw_rows)} rows, this feed has been sending at least "
                f"{min_expected_rows}. A short file looks exactly like a quiet night, "
                "and only one of the two is safe to apply."
            ),
        )
        session.add(batch)
        journal.refused(session, "intake.file", f"{adapter.name}/{filename}", batch.reason,
                        {"batch_id": batch_id, "rows": len(raw_rows)})
        session.flush()
        return ImportResult(batch_id, BatchState.HELD_FOR_REVIEW, batch.reason,
                            len(raw_rows), 0, 0, [])

    results: list[IntakeResult] = []
    for row in raw_rows:
        record = dict(zip(header, row))
        canonical_row = {name: record[spelling] for name, spelling in mapping.items()}
        results.append(apply_delivery(session, adapter, canonical_row, channel="file", batch_id=batch_id))

    applied = sum(1 for result in results if result.applied)
    quarantined = sum(1 for result in results if result.outcome is DeliveryOutcome.QUARANTINED)
    batch = ImportBatch(
        partner=adapter.name, batch_id=batch_id, filename=filename,
        row_count=len(raw_rows), applied_rows=applied, quarantined_rows=quarantined,
        state=BatchState.APPLIED.value, reason=None,
    )
    session.add(batch)
    journal.accepted(session, "intake.file", f"{adapter.name}/{filename}", None,
                     {"batch_id": batch_id, "rows": len(raw_rows),
                      "applied": applied, "quarantined": quarantined})
    session.flush()
    return ImportResult(batch_id, BatchState.APPLIED, None, len(raw_rows),
                        applied, quarantined, results)


def expected_minimum_rows(
    session: Session,
    partner: str,
    lookback: int = 7,
    fraction: float = 0.2,
) -> int:
    """A floor for tonight's file, taken from this feed's own recent history.

    A short file and a quiet night look identical from the outside. The only
    thing that tells them apart is what this partner usually sends, so that is
    what the floor is made of. Below the floor the batch is held for a human
    rather than applied, because applying a truncated file silently cancels
    every booking it failed to mention.
    """
    counts = session.execute(
        select(ImportBatch.row_count)
        .where(ImportBatch.partner == partner, ImportBatch.state == BatchState.APPLIED.value)
        .order_by(ImportBatch.received_at.desc())
        .limit(lookback)
    ).scalars().all()
    if not counts:
        return 0
    ordered = sorted(counts)
    median = ordered[len(ordered) // 2]
    return int(median * fraction)


# --------------------------------------------------------------------------- #
# internals
# --------------------------------------------------------------------------- #

def _split_trailer(content: str) -> tuple[int | None, str]:
    """Pull off a ``#rows=N`` trailer if the partner sends one."""
    lines = content.splitlines(keepends=True)
    if lines and lines[-1].strip().startswith("#rows="):
        declared = int(lines[-1].strip().removeprefix("#rows="))
        return declared, "".join(lines[:-1])
    return None, content


def _booking_values(booking: CanonicalBooking, delivery_id: int) -> dict:
    return {
        "partner": booking.partner,
        "external_id": booking.external_id,
        "status": booking.status,
        "customer_ref": booking.customer_ref,
        "product_code": booking.product_code,
        "quantity": booking.quantity,
        "starts_at": booking.starts_at,
        "ends_at": booking.ends_at,
        "amount_cents": booking.amount_cents,
        "amount_basis": booking.amount_basis,
        "currency": booking.currency,
        "updated_at_raw": booking.updated_at.raw,
        "updated_at_lo": booking.updated_at.lo,
        "updated_at_hi": booking.updated_at.hi,
        "updated_at_ambiguous": booking.updated_at.ambiguous,
        "payload_fingerprint": canonical_fingerprint(booking),
        "last_delivery_id": delivery_id,
    }


def _finish(
    session: Session,
    delivery: Delivery,
    outcome: DeliveryOutcome,
    reason: str | None,
    subject: str,
    refused: bool,
    detail: dict | None = None,
) -> IntakeResult:
    delivery.outcome = outcome.value
    delivery.outcome_reason = reason
    entry_detail = {"outcome": outcome.value, "delivery_id": delivery.id}
    if detail:
        entry_detail.update(detail)
    if refused:
        journal.refused(session, "intake.apply", subject, reason or outcome.value, entry_detail)
    else:
        journal.accepted(session, "intake.apply", subject, reason, entry_detail)
    session.flush()
    return IntakeResult(outcome, delivery.id, delivery.booking_id, reason)


def _quarantine(
    session: Session,
    adapter: "PartnerAdapter",
    raw: dict,
    channel: str,
    batch_id: str | None,
    rejection: Rejection,
) -> IntakeResult:
    delivery = Delivery(
        partner=rejection.partner,
        channel=channel,
        source_event_id=rejection.source_event_id,
        external_id=rejection.external_id,
        payload=raw,
        payload_fingerprint=None,
        updated_at_raw=None,
        batch_id=batch_id,
        outcome=DeliveryOutcome.QUARANTINED.value,
        outcome_reason=f"{rejection.kind}: {rejection.detail}",
    )
    session.add(delivery)
    session.flush()
    _raise_quarantine(
        session, rejection.partner, rejection.external_id, delivery.id,
        kind=rejection.kind, detail=rejection.detail,
        needed_from_partner=rejection.needed_from_partner,
    )
    # If this message was about a booking already held, that booking has just
    # become undecidable: something was said about it that nobody can read. It
    # stops being prepared for billing until the question is answered. That is
    # a conservative choice, and a deliberate one.
    if rejection.external_id and not mutations.is_disabled(mutations.UNDECIDED_BLOCKS_BILLING):
        known = session.scalar(
            select(Booking).where(
                Booking.partner == rejection.partner,
                Booking.external_id == rejection.external_id,
            )
        )
        if known is not None:
            known.undecided_reason = rejection.kind
            delivery.booking_id = known.id
            session.flush()
    journal.refused(
        session, "intake.apply",
        f"{rejection.partner}/{rejection.external_id or 'unknown'}",
        f"{rejection.kind}: {rejection.detail}",
        {"outcome": DeliveryOutcome.QUARANTINED.value, "delivery_id": delivery.id},
    )
    session.flush()
    # A quarantined event is an open question with the partner, so its id is
    # deliberately not marked as processed: when they resend it with the field
    # filled in, we want to look again rather than swallow the fix.
    return IntakeResult(DeliveryOutcome.QUARANTINED, delivery.id, None, rejection.detail)


def _raise_quarantine(
    session: Session,
    partner: str,
    external_id: str | None,
    delivery_id: int,
    kind: str,
    detail: str,
    needed_from_partner: str,
) -> QuarantineItem:
    item = QuarantineItem(
        partner=partner,
        external_id=external_id,
        delivery_id=delivery_id,
        kind=kind,
        detail=detail,
        needed_from_partner=needed_from_partner,
    )
    session.add(item)
    session.flush()
    return item


def _reject_batch(
    session: Session,
    adapter: "PartnerAdapter",
    batch_id: str,
    filename: str,
    rows: int,
    reason: str,
) -> ImportResult:
    batch = ImportBatch(
        partner=adapter.name, batch_id=batch_id, filename=filename,
        row_count=rows, applied_rows=0, quarantined_rows=0,
        state=BatchState.REJECTED_INCOMPLETE.value, reason=reason,
    )
    session.add(batch)
    journal.refused(session, "intake.file", f"{adapter.name}/{filename}", reason,
                    {"batch_id": batch_id, "rows": rows})
    session.flush()
    return ImportResult(batch_id, BatchState.REJECTED_INCOMPLETE, reason, rows, 0, 0, [])


def _drive_entitlement(session: Session, booking_id: int) -> None:
    from ..entitlements.service import reconcile_entitlement

    reconcile_entitlement(session, booking_id)



def resolve_and_replay(
    session: Session,
    adapter: "PartnerAdapter",
    partner: str,
    external_id: str,
    answer: str,
    replay: dict | None = None,
) -> IntakeResult | None:
    """Close an open question, then look at the held message again.

    This is the other half of holding something back, and the half that is
    usually missing. A quarantine that can only be emptied by hand is a
    quarantine that fills up until somebody turns the check off.

    ``answer`` is what the partner said, kept next to the question. ``replay``
    is the corrected payload if they resent one; without it the booking simply
    becomes decidable again and the next run bills it.
    """
    booking = session.scalar(
        select(Booking).where(Booking.partner == partner, Booking.external_id == external_id)
    )
    open_items = session.scalars(
        select(QuarantineItem).where(
            QuarantineItem.partner == partner,
            QuarantineItem.external_id == external_id,
            QuarantineItem.resolved_at.is_(None),
        )
    ).all()
    for item in open_items:
        item.resolved_at = utc_now()
        item.detail = f"{item.detail}\nanswered: {answer}"

    for conflict in session.scalars(
        select(VersionConflict)
        .where(VersionConflict.resolved_at.is_(None))
        .where(VersionConflict.booking_id == (booking.id if booking else -1))
    ).all():
        conflict.resolved_at = utc_now()

    if booking is not None:
        booking.undecided_reason = None
    journal.accepted(
        session, "intake.resolve", f"{partner}/{external_id}",
        f"question answered: {answer}",
    )
    session.flush()

    if replay is None:
        return None
    return apply_delivery(session, adapter, replay, channel="replay")
