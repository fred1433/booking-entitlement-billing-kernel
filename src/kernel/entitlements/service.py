"""The billable obligation.

Two states, on purpose. A real product has a lifecycle with claiming, sharing,
activation and expiry; this sample is not the product, and a larger lifecycle
would add states without adding a failure path. What is kept is the part
billing depends on: whether this booking is owed for, and over which window.

That the obligation follows the booking one to one is an **assumption**, not a
fact. A booking that is split, merged or partially refunded may well not map to
one obligation. It is written down as a question in
``docs/what-needs-confirmation.md`` rather than decided here.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import journal
from ..clock import utc_now
from ..models import Booking, BookingStatus, Entitlement, EntitlementState


def reconcile_entitlement(session: Session, booking_id: int) -> Entitlement | None:
    """Bring the obligation into line with the booking we now hold.

    Called after a delivery is applied, and only after: a delivery that was
    refused has changed nothing, so there is nothing to reconcile.
    """
    booking = session.get(Booking, booking_id)
    if booking is None:
        return None

    entitlement = session.scalar(
        select(Entitlement).where(Entitlement.booking_id == booking_id)
    )

    if booking.status == BookingStatus.CANCELLED.value:
        if entitlement is None:
            journal.accepted(
                session, "entitlement.skip", f"booking/{booking_id}",
                "arrived already cancelled, nothing to owe",
            )
            return None
        if entitlement.state != EntitlementState.CANCELLED.value:
            entitlement.state = EntitlementState.CANCELLED.value
            entitlement.cancelled_at = utc_now()
            entitlement.cancel_reason = "the partner cancelled the booking"
            journal.accepted(
                session, "entitlement.cancel", f"entitlement/{entitlement.id}",
                entitlement.cancel_reason,
            )
            session.flush()
        return entitlement

    if entitlement is None:
        entitlement = Entitlement(
            booking_id=booking_id,
            state=EntitlementState.ACTIVE.value,
            billable_from=booking.starts_at,
            billable_to=booking.ends_at,
        )
        session.add(entitlement)
        session.flush()
        journal.accepted(session, "entitlement.create", f"entitlement/{entitlement.id}")
        return entitlement

    if entitlement.state == EntitlementState.CANCELLED.value:
        # A cancelled obligation is not reopened by a later amendment. Whether
        # a partner is allowed to un-cancel at all is a question for them, not
        # a default to pick here.
        journal.refused(
            session, "entitlement.reopen", f"entitlement/{entitlement.id}",
            "already cancelled, a later amendment does not reopen it",
        )
        return entitlement

    entitlement.billable_from = booking.starts_at
    entitlement.billable_to = booking.ends_at
    session.flush()
    return entitlement
