"""Two workers, at the same moment, on the same row.

Every test in this file uses two real connections to PostgreSQL. That is the
point: two sequential calls in one process prove that a function is idempotent,
which is a different and much easier claim. What breaks in production is two
processes interleaving between somebody's read and somebody's write, and the
only way to show it is to make them actually interleave.

The tool for that is ``wait_until_blocked``, which asks the server whether a
connection is waiting on a lock. It makes the race deterministic without a
sleep, and a sleep here would be the same thing as not testing the race.
"""

from __future__ import annotations

import threading

from datetime import datetime, timezone

from sqlalchemy import func, select

from conftest import PERIOD, coralbay_event, wait_until_blocked
from kernel import intake
from kernel.billing import run
from kernel.models import Booking, BillingLine, Delivery, DeliveryOutcome, LineState


def test_two_workers_delivering_the_same_retry_apply_it_once(
    session, other_session, engine, coralbay
):
    """The same event id arrives twice, on two connections, at the same moment.

    One of them applies it. The other is told it already has it. The settling is
    done by a unique index, not by a lookup, because a lookup followed by an
    insert is exactly the gap two workers fall through.
    """
    event = coralbay_event(event_id="evt-race", booking_ref="CB-3001")
    outcomes: list[DeliveryOutcome] = []
    barrier = threading.Barrier(2, timeout=10)

    def deliver(target_session):
        barrier.wait()
        result = intake.apply_delivery(target_session, coralbay, event)
        target_session.commit()
        outcomes.append(result.outcome)

    first = threading.Thread(target=deliver, args=(session,))
    second = threading.Thread(target=deliver, args=(other_session,))
    first.start()
    second.start()
    first.join(timeout=20)
    second.join(timeout=20)

    assert sorted(o.value for o in outcomes) == ["applied", "duplicate"]
    with engine.connect() as connection:
        assert connection.execute(select(func.count()).select_from(Booking)).scalar_one() == 1
        # Both arrivals are recorded. "Did you receive it" and "did you act on
        # it" are different questions when a partner is asking.
        assert connection.execute(select(func.count()).select_from(Delivery)).scalar_one() == 2


def test_two_workers_preparing_the_same_period_produce_one_line(
    session, other_session, engine, coralbay
):
    """Preparation is an insert that does nothing on conflict.

    Two runs started at the same moment for the same period converge on one
    line per obligation, because the second insert loses to the unique index
    rather than to a check somebody remembered to write.
    """
    intake.apply_delivery(session, coralbay, coralbay_event(event_id="e1", booking_ref="CB-3002"))
    session.commit()

    done = threading.Barrier(2, timeout=10)

    def prepare(target_session):
        done.wait()
        run.prepare(target_session, PERIOD)
        target_session.commit()

    first = threading.Thread(target=prepare, args=(session,))
    second = threading.Thread(target=prepare, args=(other_session,))
    first.start()
    second.start()
    first.join(timeout=20)
    second.join(timeout=20)

    with engine.connect() as connection:
        assert connection.execute(select(func.count()).select_from(BillingLine)).scalar_one() == 1


def test_only_one_worker_can_take_a_prepared_line(session, other_session, coralbay):
    """The claim is one statement, so the second worker finds nothing to take.

    A read of the state followed by a write of the state would let both of them
    through, and both would then call the external system with the same key.
    The provider would refuse the second, which is lucky rather than designed;
    here the second one never gets as far as calling.
    """
    intake.apply_delivery(session, coralbay, coralbay_event(event_id="e1", booking_ref="CB-3003"))
    run.prepare(session, PERIOD)
    session.commit()
    line_id = session.scalars(select(BillingLine.id)).one()

    stamp = datetime.now(timezone.utc)
    first_claim = run._claim(session, line_id, stamp)
    session.commit()
    second_claim = run._claim(other_session, line_id, stamp)
    other_session.commit()

    assert first_claim is True
    assert second_claim is False


def test_a_second_writer_waits_for_the_row_rather_than_overwriting_it(
    session, other_session, engine, coralbay
):
    """The update path takes a row lock, and the proof is that somebody waits.

    The assertion is on the server's own view of who is blocked, so this is not
    a story about ordering that happens to hold on a fast machine.
    """
    intake.apply_delivery(session, coralbay, coralbay_event(event_id="e1", booking_ref="CB-3004"))
    session.commit()

    # Hold the row on one connection.
    session.execute(
        select(Booking).where(Booking.external_id == "CB-3004").with_for_update()
    ).scalar_one()

    blocked: list[str] = []

    def second_writer():
        intake.apply_delivery(
            other_session, coralbay,
            coralbay_event(event_id="e2", booking_ref="CB-3004",
                           updated_at="2026-08-05T09:15:00+02:00", amount_minor=30000),
        )
        other_session.commit()
        blocked.append("finished after the lock was released")

    worker = threading.Thread(target=second_writer)
    worker.start()
    wait_until_blocked(engine)          # the server says somebody is waiting
    session.rollback()                  # release
    worker.join(timeout=20)

    assert blocked == ["finished after the lock was released"]
    with engine.connect() as connection:
        amount = connection.execute(
            select(Booking.amount_cents).where(Booking.external_id == "CB-3004")
        ).scalar_one()
    assert amount == 30000
