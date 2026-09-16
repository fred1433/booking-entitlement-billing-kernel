"""Entitlement lifecycle."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import journal
from ..clock import utc_now
from ..config import MAX_SHARES_PER_ENTITLEMENT
from ..models import (
    Booking,
    BookingStatus,
    Entitlement,
    EntitlementShare,
    EntitlementState,
)
from .state import TERMINAL, is_allowed


class IllegalTransition(RuntimeError):
    def __init__(self, entitlement_id: int, current: str, target: str) -> None:
        super().__init__(f"entitlement {entitlement_id}: {current} cannot become {target}")
        self.entitlement_id = entitlement_id
        self.current = current
        self.target = target


class ShareLimitReached(RuntimeError):
    pass


def transition(session: Session, entitlement: Entitlement, target: str,
               reason: str | None = None) -> Entitlement:
    """Move an entitlement, or refuse in writing."""
    current = entitlement.state
    if not is_allowed(current, target):
        journal.refused(
            session, "entitlement.transition", f"entitlement/{entitlement.id}",
            f"{current} cannot become {target}",
            {"from": current, "to": target, "terminal": current in TERMINAL},
        )
        session.flush()
        raise IllegalTransition(entitlement.id, current, target)

    entitlement.state = target
    now = utc_now()
    if target == EntitlementState.CLAIMABLE.value:
        entitlement.claimable_at = now
    elif target == EntitlementState.CLAIMED.value:
        entitlement.claimed_at = now
    elif target == EntitlementState.ACTIVE.value:
        entitlement.activated_at = now
    elif target == EntitlementState.CANCELLED.value:
        entitlement.cancelled_at = now
        entitlement.cancel_reason = reason
    elif target == EntitlementState.EXPIRED.value:
        entitlement.expires_at = entitlement.expires_at or now

    journal.accepted(session, "entitlement.transition", f"entitlement/{entitlement.id}",
                     reason, {"from": current, "to": target})
    session.flush()
    return entitlement


def reconcile_entitlement(session: Session, booking_id: int) -> Entitlement | None:
    """Bring the entitlement in line with the booking we now hold.

    Called after every applied delivery. It is deliberately the only path that
    creates or cancels an entitlement from partner data, so there is one answer
    to "why is this entitlement in this state" and it is in the journal.
    """
    booking = session.get(Booking, booking_id)
    if booking is None:
        return None

    entitlement = session.execute(
        select(Entitlement).where(Entitlement.booking_id == booking_id).with_for_update()
    ).scalar_one_or_none()

    if entitlement is None:
        if booking.status == BookingStatus.CANCELLED.value:
            journal.accepted(
                session, "entitlement.create", f"{booking.partner}/{booking.external_id}",
                "booking arrived already cancelled, no entitlement created",
            )
            session.flush()
            return None
        entitlement = Entitlement(
            booking_id=booking.id,
            state=EntitlementState.CREATED.value,
            holder_ref=booking.customer_ref,
            billable_from=booking.starts_at,
            billable_to=booking.ends_at,
        )
        session.add(entitlement)
        session.flush()
        journal.accepted(session, "entitlement.create", f"entitlement/{entitlement.id}",
                         None, {"booking": f"{booking.partner}/{booking.external_id}"})
        transition(session, entitlement, EntitlementState.CLAIMABLE.value,
                   "booking confirmed by the partner")
        return entitlement

    if booking.status == BookingStatus.CANCELLED.value and entitlement.state not in TERMINAL:
        cancel(session, entitlement, "the partner cancelled the booking")
        return entitlement

    # An amendment moves the billable window. It never resurrects a terminal
    # entitlement: a cancelled booking that comes back is a new booking, and
    # saying so out loud is cheaper than discovering it on an invoice.
    if entitlement.state not in TERMINAL:
        entitlement.billable_from = booking.starts_at
        entitlement.billable_to = booking.ends_at
        entitlement.holder_ref = booking.customer_ref
        session.flush()
    return entitlement


def cancel(session: Session, entitlement: Entitlement, reason: str) -> Entitlement:
    from ..billing.run import on_entitlement_cancelled

    transition(session, entitlement, EntitlementState.CANCELLED.value, reason)
    on_entitlement_cancelled(session, entitlement)
    return entitlement


def activate(session: Session, entitlement: Entitlement) -> Entitlement:
    return transition(session, entitlement, EntitlementState.ACTIVE.value,
                      "first use by the holder")


def share(session: Session, entitlement: Entitlement, shared_with: str) -> EntitlementShare:
    """Add one share, inside a bound the database enforces.

    The next free slot is read, then written. If two requests read the same
    free slot, the unique index lets exactly one of them keep it and the other
    is refused, which is the behaviour you want and not the behaviour a count
    in Python gives you.
    """
    used = session.execute(
        select(EntitlementShare.share_index).where(
            EntitlementShare.entitlement_id == entitlement.id
        )
    ).scalars().all()
    free = next((slot for slot in range(1, MAX_SHARES_PER_ENTITLEMENT + 1) if slot not in used), None)
    if free is None:
        journal.refused(
            session, "entitlement.share", f"entitlement/{entitlement.id}",
            f"all {MAX_SHARES_PER_ENTITLEMENT} share slots are taken",
            {"shared_with": shared_with},
        )
        session.flush()
        raise ShareLimitReached(f"entitlement {entitlement.id} already has {len(used)} shares")

    record = EntitlementShare(entitlement_id=entitlement.id, share_index=free, shared_with=shared_with)
    session.add(record)
    try:
        session.flush()
    except IntegrityError as error:
        session.rollback()
        raise ShareLimitReached(str(error)) from error

    journal.accepted(session, "entitlement.share", f"entitlement/{entitlement.id}",
                     None, {"slot": free, "shared_with": shared_with})
    session.flush()
    return record


def expire_due(session: Session, now: datetime | None = None) -> list[Entitlement]:
    now = now or utc_now()
    due = session.execute(
        select(Entitlement).where(
            Entitlement.expires_at.is_not(None),
            Entitlement.expires_at <= now,
            Entitlement.state.not_in(tuple(TERMINAL)),
        )
    ).scalars().all()
    for entitlement in due:
        transition(session, entitlement, EntitlementState.EXPIRED.value, "expiry date reached")
    return list(due)
