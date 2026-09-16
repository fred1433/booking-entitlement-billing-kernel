"""Partner A: Coralbay Reservations. Fictional.

Behaviour this adapter is written against, all of it invented for this
repository, all of it borrowed from things real booking partners do:

* at-least-once webhooks, so the same ``event_id`` arrives two or three times;
* a retry re-serialises the envelope, so the retried copy can carry a fresher
  ``updated_at`` than the copy that was delivered first, while describing the
  same event;
* timestamps always carry an offset, which makes them orderable;
* a status vocabulary close enough to ours to be tempting, and one value
  (``pending``) that means something we have no equivalent for;
* ``amount_minor`` is the price per person for the whole stay, which this
  fictional partner has confirmed in writing. Whether a money field is per unit
  or per booking is the single most expensive thing on this list to assume, and
  it is invisible in the data: every payload parses either way, and the invoice
  is wrong by a factor of the party size.
"""

from __future__ import annotations

from datetime import datetime

from ..clock import parse_partner_timestamp
from ..intake.schemas import CanonicalBooking, Rejection
from ..models import BookingStatus

CORALBAY = "coralbay"

#: Declared, not observed. Coralbay's ``amount_minor`` was described to us as
#: the price of one unit, so a booking for two costs twice it.
#: The question that would change this: "is amount_minor the price per pax, or
#: the total for the booking?" Nothing in the payload answers it, and the two
#: readings differ by the quantity on every invoice.
AMOUNT_BASIS = "per_unit"

_STATUS = {
    "confirmed": BookingStatus.CONFIRMED.value,
    "amended": BookingStatus.AMENDED.value,
    "cancelled": BookingStatus.CANCELLED.value,
}

_REQUIRED = (
    "event_id",
    "booking_ref",
    "state",
    "guest_reference",
    "rate_plan",
    "pax",
    "check_in",
    "check_out",
    "amount_minor",
    "currency",
    "updated_at",
)


class CoralbayAdapter:
    name = CORALBAY
    display_name = "Coralbay Reservations (fictional)"
    declared_timezone = None            # always sends an offset

    def resolve_columns(self, header: list[str]) -> tuple[dict[str, str], list[str]]:
        raise NotImplementedError(
            "Coralbay is a push feed. It has no file shape, and asking for one "
            "means an adapter was passed to the wrong intake path."
        )

    def normalise(self, raw: dict) -> CanonicalBooking | Rejection:
        external_id = raw.get("booking_ref")
        event_id = raw.get("event_id")

        missing = [field for field in _REQUIRED if raw.get(field) in (None, "")]
        if missing:
            return Rejection(
                partner=self.name,
                external_id=external_id,
                source_event_id=event_id,
                kind="missing_field",
                detail=f"missing or empty: {', '.join(missing)}",
                needed_from_partner=(
                    "Confirm whether these fields are ever optional, and what the "
                    "kernel should do when they are absent. Until then the event is "
                    "held, not defaulted."
                ),
            )

        state = str(raw["state"]).strip().lower()
        if state not in _STATUS:
            return Rejection(
                partner=self.name,
                external_id=external_id,
                source_event_id=event_id,
                kind="unknown_status_value",
                detail=f"state={raw['state']!r} is not in the agreed vocabulary",
                needed_from_partner=(
                    f"What does state={raw['state']!r} mean, and which of confirmed, "
                    "amended or cancelled should it map to? Guessing here is how an "
                    "entitlement gets activated for a booking that was never paid."
                ),
            )

        updated_at = parse_partner_timestamp(str(raw["updated_at"]), self.declared_timezone)
        if not updated_at.usable:
            return Rejection(
                partner=self.name,
                external_id=external_id,
                source_event_id=event_id,
                kind=f"timestamp_{updated_at.problem.value}",
                detail=f"updated_at={raw['updated_at']!r}",
                needed_from_partner=(
                    "Send updated_at as an ISO 8601 timestamp with an offset. "
                    "Without it we cannot order two updates to the same booking."
                ),
            )

        try:
            starts_at = datetime.fromisoformat(str(raw["check_in"]).replace("Z", "+00:00"))
            ends_at = datetime.fromisoformat(str(raw["check_out"]).replace("Z", "+00:00"))
            quantity = int(raw["pax"])
            amount = int(raw["amount_minor"])
        except (TypeError, ValueError) as error:
            return Rejection(
                partner=self.name,
                external_id=external_id,
                source_event_id=event_id,
                kind="unreadable_field",
                detail=str(error),
                needed_from_partner="Confirm the types of check_in, check_out, pax and amount_minor.",
            )

        if starts_at.tzinfo is None or ends_at.tzinfo is None:
            return Rejection(
                partner=self.name,
                external_id=external_id,
                source_event_id=event_id,
                kind="timestamp_no_zone_declared",
                detail="check_in or check_out has no offset",
                needed_from_partner="Send check_in and check_out with an offset.",
            )

        return CanonicalBooking(
            partner=self.name,
            external_id=str(external_id),
            source_event_id=str(event_id),
            status=_STATUS[state],
            customer_ref=str(raw["guest_reference"]),
            product_code=str(raw["rate_plan"]),
            quantity=quantity,
            starts_at=starts_at,
            ends_at=ends_at,
            amount_cents=amount,
            amount_basis=AMOUNT_BASIS,
            currency=str(raw["currency"]).upper(),
            updated_at=updated_at,
        )
