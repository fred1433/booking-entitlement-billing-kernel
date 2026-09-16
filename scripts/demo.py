"""One run, printed as the sample saw it.

Everything here is synthetic. Both partners are fictional, the external payment
system is a simulation with its own table, and no network call is made.

The sequence is chosen to put the four interesting refusals next to each other:
a version collision that nobody can settle, a value nobody declared, an
operation that succeeded while its answer was lost, and a cancellation that
arrives after the money has already moved.
"""

from __future__ import annotations

import os
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sqlalchemy import select, text                                   # noqa: E402

from kernel import intake                                             # noqa: E402
from kernel.billing import reconciliation, run                        # noqa: E402
from kernel.billing.payments import SimulatedProvider                 # noqa: E402
from kernel.db import make_engine, make_session_factory                # noqa: E402
from kernel.models import Base, BillingLine, JournalEntry              # noqa: E402
from kernel.partners import CoralbayAdapter, LindhoffAdapter           # noqa: E402

PERIOD = "2026-08"
STAMP = "2026-08-04T09:15:00+02:00"


def event(event_id, ref, **over):
    payload = {
        "event_id": event_id, "booking_ref": ref, "state": "confirmed",
        "guest_reference": "guest-77", "rate_plan": "SEA-VIEW-FLEX", "pax": 2,
        "check_in": "2026-08-10T14:00:00+02:00", "check_out": "2026-08-14T10:00:00+02:00",
        "amount_minor": 24000, "currency": "EUR", "updated_at": STAMP,
    }
    payload.update(over)
    return payload


def row(ref, **over):
    record = {
        "external_ref": ref, "status": "OK", "cust": "cust-31", "article": "CITY-PASS-72H",
        "units": 1, "from": "2026-08-10", "to": "2026-08-13",
        "price_cents": 18000, "curr": "EUR", "changed_at": "2026-08-04 09:15:00",
    }
    record.update(over)
    return record


def say(line: str) -> None:
    print(f"  {line}")


def main() -> int:
    engine = make_engine(os.environ.get("DATABASE_URL"))
    sessions = make_session_factory(engine)
    with engine.begin() as connection:
        names = ", ".join(f'"{name}"' for name in Base.metadata.tables)
        connection.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))

    coralbay, lindhoff = CoralbayAdapter(), LindhoffAdapter()
    provider = SimulatedProvider(sessions)

    print("one run of synthetic partner traffic")
    print("both partners are fictional, and the payment provider is a simulation")
    print()

    with sessions() as session:
        say("Coralbay pushes two bookings, and retries the first one twice")
        intake.apply_delivery(session, coralbay, event("evt-1", "CB-1000"))
        intake.apply_delivery(session, coralbay, event("evt-2", "CB-1001", amount_minor=48000))
        intake.apply_delivery(session, coralbay, event("evt-1", "CB-1000"))
        intake.apply_delivery(session, coralbay, event("evt-1", "CB-1000"))

        say("an older copy of an update arrives after the newer one")
        intake.apply_delivery(session, coralbay, event(
            "evt-3", "CB-1001", updated_at="2026-08-05T09:15:00+02:00", amount_minor=52000))
        intake.apply_delivery(session, coralbay, event(
            "evt-4", "CB-1001", updated_at="2026-08-03T09:15:00+02:00", amount_minor=10000))

        say("two versions of one booking arrive carrying the same timestamp")
        intake.apply_delivery(session, coralbay, event("evt-5", "CB-1002", pax=2))
        intake.apply_delivery(session, coralbay, event("evt-6", "CB-1002", pax=9))

        say("Lindhoff drops its nightly file, and one row carries a status nobody declared")
        intake.import_file(session, lindhoff, "2026-08-04.csv", _csv([row("LH-2000")]))
        intake.apply_delivery(session, lindhoff, row("LH-2000", status="HOLD"), channel="file")
        session.commit()

    with sessions() as session:
        say("the monthly run prepares what it is allowed to prepare")
        run.prepare(session, PERIOD)
        session.commit()

    say("the worker submits, and the answer to one operation never comes back")
    lost_once = _once()
    provider.lose_response_for = lost_once
    run.submit(sessions, provider, PERIOD)

    say("the worker restarts and resumes: the same key, the same parameters")
    provider.lose_response_for = None
    run.resume(sessions, provider, PERIOD)

    with sessions() as session:
        say("one booking is cancelled, after its operation was already established")
        line = session.scalars(
            select(BillingLine).where(BillingLine.state == "charged").order_by(BillingLine.id)
        ).first()
        if line is not None:
            run.cancel(session, line.entitlement_id, "the partner cancelled")
        session.commit()

    print()
    print()
    print("journal")
    print()
    with sessions() as session:
        entries = session.scalars(select(JournalEntry).order_by(JournalEntry.id)).all()
        refusals = 0
        for entry in entries:
            if entry.outcome == "refused":
                refusals += 1
            reason = f"   {entry.reason}" if entry.reason else ""
            print(f"  {entry.outcome:<9} {entry.action:<24} {entry.subject:<24}{reason}")
        print()
        print(f"  {len(entries)} entries, {refusals} of them refusals")
        print(f"  simulated provider: {provider.attempts} calls, "
              f"{provider.total_operations()} operations actually carried out")

    print()
    print()
    with sessions() as session:
        report = reconciliation.reconcile(session, PERIOD)
        print(reconciliation.render_reconciliation(session, report))

    engine.dispose()
    return 0


def _once():
    seen: list[str] = []

    def lose(key: str) -> bool:
        seen.append(key)
        return len(seen) == 1

    return lose


def _csv(rows):
    header = ["external_ref", "status", "cust", "article", "units",
              "from", "to", "price_cents", "curr", "changed_at"]
    lines = [",".join(header)]
    for record in rows:
        lines.append(",".join(str(record[column]) for column in header))
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    raise SystemExit(main())
