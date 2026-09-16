"""Partner B: Lindhoff Booking Suite. Fictional.

Behaviour this adapter is written against, all of it invented for this
repository, all of it borrowed from things real booking partners do:

* one nightly CSV rather than events, rows in no particular order;
* ``changed_at`` written as a naive local time in the partner's own zone, so
  one hour a year contains two of every reading and one hour a year contains
  none;
* a status vocabulary of its own: OK, CHG, VOID;
* a column that was renamed from ``ext_ref`` to ``external_ref`` at some point,
  so both spellings are in circulation. The adapter accepts both because both
  are written down. A third spelling is refused rather than guessed.
"""

from __future__ import annotations

from datetime import datetime, timezone

from ..clock import parse_partner_timestamp
from ..intake.schemas import CanonicalBooking, Rejection
from ..models import BookingStatus

LINDHOFF = "lindhoff"

#: Declared, not observed, and deliberately different from the other feed.
#: Lindhoff's ``price_cents`` was described to us as the total for the row,
#: already multiplied by ``units``. Read it the other way and every invoice
#: from this feed is wrong by the quantity, and every payload still parses.
#: The question that would change this: "does price_cents already include
#: units, or is it the price of one?"
AMOUNT_BASIS = "per_booking"

_STATUS = {
    "OK": BookingStatus.CONFIRMED.value,
    "CHG": BookingStatus.AMENDED.value,
    "VOID": BookingStatus.CANCELLED.value,
}

#: Column aliases the partner has confirmed in writing. Anything outside this
#: map is an unknown file shape, not a near miss to be resolved by similarity.
COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "external_ref": ("external_ref", "ext_ref"),
    "status": ("status",),
    "cust": ("cust",),
    "article": ("article",),
    "units": ("units",),
    "from": ("from",),
    "to": ("to",),
    "price_cents": ("price_cents",),
    "curr": ("curr",),
    "changed_at": ("changed_at",),
}

REQUIRED_COLUMNS = tuple(COLUMN_ALIASES)


def resolve_columns(header: list[str]) -> tuple[dict[str, str], list[str]]:
    """Map each canonical column onto the spelling this file actually uses."""
    present = {name.strip() for name in header}
    mapping: dict[str, str] = {}
    unresolved: list[str] = []
    for canonical, spellings in COLUMN_ALIASES.items():
        found = next((spelling for spelling in spellings if spelling in present), None)
        if found is None:
            unresolved.append(canonical)
        else:
            mapping[canonical] = found
    return mapping, unresolved


class LindhoffAdapter:
    name = LINDHOFF
    display_name = "Lindhoff Booking Suite (fictional)"
    declared_timezone = "Europe/Amsterdam"

    def resolve_columns(self, header: list[str]) -> tuple[dict[str, str], list[str]]:
        return resolve_columns(header)

    def normalise(self, raw: dict) -> CanonicalBooking | Rejection:
        external_id = raw.get("external_ref") or raw.get("ext_ref")

        status_raw = str(raw.get("status", "")).strip().upper()
        if status_raw not in _STATUS:
            return Rejection(
                partner=self.name,
                external_id=external_id,
                source_event_id=None,
                kind="unknown_status_value",
                detail=f"status={raw.get('status')!r} is not one of OK, CHG, VOID",
                needed_from_partner=(
                    f"What does status={raw.get('status')!r} mean? It is not in the "
                    "three values we agreed. The rows carrying it are held until "
                    "you tell us, rather than mapped to the nearest thing."
                ),
            )

        updated_at = parse_partner_timestamp(str(raw.get("changed_at", "")), self.declared_timezone)
        if not updated_at.usable:
            needed = {
                "nonexistent_local_time": (
                    "changed_at points at a local wall clock reading that does not "
                    "exist: the clock jumped over it when summer time started. Send "
                    "changed_at in UTC, or with an offset, and this class of row "
                    "disappears for good."
                ),
                "no_zone_declared": (
                    "changed_at has no offset and we have no declared zone for this "
                    "feed. Confirm the zone in writing, or send UTC."
                ),
                "not_parseable": "changed_at is not a timestamp we can read. Confirm the format.",
            }[updated_at.problem.value]
            return Rejection(
                partner=self.name,
                external_id=external_id,
                source_event_id=None,
                kind=f"timestamp_{updated_at.problem.value}",
                detail=f"changed_at={raw.get('changed_at')!r}",
                needed_from_partner=needed,
            )

        try:
            starts_at = _local_day(raw["from"], self.declared_timezone)
            ends_at = _local_day(raw["to"], self.declared_timezone)
            quantity = int(raw["units"])
            amount = int(raw["price_cents"])
        except (KeyError, TypeError, ValueError) as error:
            return Rejection(
                partner=self.name,
                external_id=external_id,
                source_event_id=None,
                kind="unreadable_field",
                detail=str(error),
                needed_from_partner="Confirm the types of from, to, units and price_cents.",
            )

        return CanonicalBooking(
            partner=self.name,
            external_id=str(external_id),
            source_event_id=None,           # this feed has no event id at all
            status=_STATUS[status_raw],
            customer_ref=str(raw["cust"]),
            product_code=str(raw["article"]),
            quantity=quantity,
            starts_at=starts_at,
            ends_at=ends_at,
            amount_cents=amount,
            amount_basis=AMOUNT_BASIS,
            currency=str(raw["curr"]).upper(),
            updated_at=updated_at,
        )


def _local_day(value: object, zone_name: str) -> datetime:
    """A calendar day in the partner's zone, kept as the instant it starts."""
    from zoneinfo import ZoneInfo

    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(zone_name))
    return parsed.astimezone(timezone.utc)
