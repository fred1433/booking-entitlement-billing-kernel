"""The kernel's own vocabulary, and the only shape the core ever sees.

Every partner writes bookings differently. Exactly one place in this codebase
is allowed to know that: the adapter. Past the adapter there is one envelope,
and the core never asks which partner a record came from in order to decide
what a field means.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime

from ..clock import Instant


@dataclass(frozen=True)
class CanonicalBooking:
    partner: str
    external_id: str
    source_event_id: str | None
    status: str                    # BookingStatus value
    customer_ref: str
    product_code: str
    quantity: int
    starts_at: datetime
    ends_at: datetime
    unit_amount_cents: int
    currency: str
    updated_at: Instant


@dataclass(frozen=True)
class Rejection:
    """An input the adapter refuses to guess at.

    ``needed_from_partner`` is written to be pasted into a message to the
    partner, because that is what it is for.
    """

    partner: str
    external_id: str | None
    source_event_id: str | None
    kind: str
    detail: str
    needed_from_partner: str


def canonical_fingerprint(booking: CanonicalBooking) -> str:
    """A hash of what the business cares about, and of nothing else.

    The timestamp is deliberately not part of it. That is what lets the kernel
    tell a partner how many of last night's updates actually changed something,
    and it is what makes two deliveries carrying the same state at the same
    timestamp a duplicate rather than a conflict.
    """
    material = json.dumps(
        {
            "status": booking.status,
            "customer_ref": booking.customer_ref,
            "product_code": booking.product_code,
            "quantity": booking.quantity,
            "starts_at": booking.starts_at.isoformat(),
            "ends_at": booking.ends_at.isoformat(),
            "unit_amount_cents": booking.unit_amount_cents,
            "currency": booking.currency,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()
