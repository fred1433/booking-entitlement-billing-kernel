"""One night of partner traffic, end to end, printed as the kernel saw it.

Run it against an empty database and read the journal. The interesting lines
are the refusals: every one of them is a place where a kernel without that
control would have changed something it should not have.

    python scripts/demo.py

Both partners are fictional.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sqlalchemy import select, text                                    # noqa: E402

from kernel.billing import (                                           # noqa: E402
    FakeStripe,
    charge_prepared,
    prepare,
    reconcile,
    render_reconciliation,
)
from kernel.config import Settings                                     # noqa: E402
from kernel.db import make_engine, make_session_factory                # noqa: E402
from kernel.entitlements import consume_claim_token, issue_claim_token  # noqa: E402
from kernel.intake import apply_delivery, import_file                  # noqa: E402
from kernel.models import (                                            # noqa: E402
    Base,
    BillingLine,
    Booking,
    Entitlement,
    JournalEntry,
)
from kernel.partners import CoralbayAdapter, LindhoffAdapter           # noqa: E402

PERIOD = "2026-08"
SETTINGS = Settings(
    database_url=os.environ.get("DATABASE_URL", Settings.from_env().database_url),
    claim_token_secret="demo-secret-only-for-this-script",
    claim_token_ttl_seconds=3600,
    share_limit=3,
)


def booking(reference: str, event_id: str, state: str = "confirmed",
            updated_at: str = "2026-08-04T09:00:00+02:00", amount: int = 24000) -> dict:
    return {
        "event_id": event_id,
        "booking_ref": reference,
        "state": state,
        "guest_reference": f"guest-{reference[-3:]}",
        "rate_plan": "SEA-VIEW-FLEX",
        "pax": 2,
        "check_in": "2026-08-10T14:00:00+02:00",
        "check_out": "2026-08-14T10:00:00+02:00",
        "amount_minor": amount,
        "currency": "EUR",
        "updated_at": updated_at,
    }


def csv_file(rows: list[list[str]]) -> str:
    header = "external_ref,status,cust,article,units,from,to,price_cents,curr,changed_at"
    return header + "\n" + "\n".join(",".join(row) for row in rows) + "\n"


def step(title: str) -> None:
    print(f"\n  {title}")


def main() -> int:
    engine = make_engine(SETTINGS.database_url)
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        tables = ", ".join(f'"{name}"' for name in Base.metadata.tables)
        connection.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))

    factory = make_session_factory(engine)
    session = factory()
    coralbay, lindhoff = CoralbayAdapter(), LindhoffAdapter()

    print("one night of partner traffic, through the kernel")
    print("both partners are fictional")

    step("Coralbay pushes three bookings, and retries one of them twice")
    for index in range(3):
        apply_delivery(session, coralbay, booking(f"CB-100{index}", f"evt-a{index}"))
    apply_delivery(session, coralbay, booking("CB-1000", "evt-a0"))
    apply_delivery(session, coralbay, booking("CB-1000", "evt-a0"))

    step("an old copy of an update arrives after the new one")
    apply_delivery(session, coralbay, booking(
        "CB-1002", "evt-b2", state="amended", updated_at="2026-08-04T11:00:00+02:00", amount=26000))
    apply_delivery(session, coralbay, booking(
        "CB-1002", "evt-b3", state="confirmed", updated_at="2026-08-04T08:00:00+02:00"))

    step("Lindhoff drops its nightly file: one good row, one unknown status, "
         "two readings inside the hour the clocks go back")
    import_file(session, lindhoff, "lindhoff-2026-08-04.csv", csv_file([
        ["LH-2000", "OK", "cust-1", "CITY-PASS-72H", "1", "2026-08-10", "2026-08-13",
         "18000", "EUR", "2026-08-04 02:10:00"],
        ["LH-2001", "HOLD", "cust-2", "CITY-PASS-72H", "1", "2026-08-10", "2026-08-13",
         "18000", "EUR", "2026-08-04 02:20:00"],
        ["LH-2002", "OK", "cust-3", "CITY-PASS-72H", "1", "2026-08-10", "2026-08-13",
         "15000", "EUR", "2026-10-25 02:30:00"],
    ]))
    apply_delivery(session, lindhoff, {
        "external_ref": "LH-2002", "status": "CHG", "cust": "cust-3",
        "article": "CITY-PASS-72H", "units": "1", "from": "2026-08-10", "to": "2026-08-13",
        "price_cents": "17000", "curr": "EUR", "changed_at": "2026-10-25 02:45:00",
    }, channel="file")
    session.commit()

    step("every holder claims their entitlement, each link spent once")
    for entitlement in session.execute(select(Entitlement)).scalars().all():
        token = issue_claim_token(session, entitlement, SETTINGS)
        consume_claim_token(session, token, f"holder-{entitlement.id}", SETTINGS)
    session.commit()

    step("the monthly run starts, the worker dies after the second charge, and the "
         "third charge is taken but its answer never comes back")
    provider = FakeStripe()
    result = prepare(session, PERIOD)
    lost = session.execute(
        select(BillingLine.idempotency_key)
        .join(Entitlement, Entitlement.id == BillingLine.entitlement_id)
        .join(Booking, Booking.id == Entitlement.booking_id)
        .where(Booking.external_id == "CB-1001")
    ).scalar_one()
    provider.fail_before_response = lambda key: key == lost
    charged: list[int] = []

    class Restarted(RuntimeError):
        pass

    def die_after_two(line_id: int) -> None:
        charged.append(line_id)
        if len(charged) == 2:
            raise Restarted

    try:
        charge_prepared(session, PERIOD, provider, result, after_each=die_after_two)
    except Restarted:
        session.rollback()

    step("the run is started again, and the charge whose answer was lost is "
         "presented under the same key rather than taken a second time")
    provider.fail_before_response = None
    charge_prepared(session, PERIOD, provider)
    session.commit()

    step("one booking is cancelled after its charge was taken")
    apply_delivery(session, coralbay, booking(
        "CB-1001", "evt-c1", state="cancelled", updated_at="2026-08-05T09:00:00+02:00"))
    session.commit()

    print("\n\njournal\n")
    entries = session.execute(select(JournalEntry).order_by(JournalEntry.id)).scalars().all()
    for entry in entries:
        marker = "refused " if entry.outcome == "refused" else "accepted"
        reason = f"  {entry.reason}" if entry.reason else ""
        print(f"  {marker}  {entry.action:<24} {entry.subject:<24}{reason}".rstrip())

    refusals = sum(1 for entry in entries if entry.outcome == "refused")
    print(f"\n  {len(entries)} entries, {refusals} of them refusals")
    print(f"  payment provider: {provider.attempts} calls, {provider.charges} charges")

    print("\n\n" + render_reconciliation(reconcile(session, PERIOD)))
    session.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
