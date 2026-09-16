"""One property, checked against every order the network can invent.

Everything else in this suite is a case somebody thought of. This is the part
that looks for the case nobody thought of: whatever order the deliveries arrive
in, and however many times each one is repeated, the booking ends up in the
same state, and that state is the newest one the partner sent.

If that property holds, replays are free, pull windows can overlap, and a
failed batch can simply be sent again. Every operational habit in the README
rests on it, so it is worth checking with something other than an opinion.
"""

from __future__ import annotations

from hypothesis import HealthCheck, given
from hypothesis import settings as hypothesis_settings
from hypothesis import strategies as st
from sqlalchemy import select

from conftest import coralbay_event, truncate_all
from kernel.intake import apply_delivery
from kernel.models import Booking, DeliveryOutcome

TIMELINE = [
    ("evt-0", "confirmed", "2026-08-04T09:00:00+02:00", 20000),
    ("evt-1", "amended", "2026-08-04T10:00:00+02:00", 22000),
    ("evt-2", "amended", "2026-08-04T11:00:00+02:00", 25000),
    ("evt-3", "cancelled", "2026-08-04T12:00:00+02:00", 25000),
]
NEWEST = TIMELINE[-1]


def _event(index: int) -> dict:
    event_id, state, updated_at, amount = TIMELINE[index]
    return coralbay_event(event_id=event_id, booking_ref="CB-PROP", state=state,
                          updated_at=updated_at, amount_minor=amount)


@given(
    order=st.permutations(range(len(TIMELINE))),
    redeliveries=st.lists(st.integers(min_value=0, max_value=len(TIMELINE) - 1), max_size=6),
)
@hypothesis_settings(
    max_examples=60,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_any_order_and_any_number_of_redeliveries_reach_the_same_state(
    session, engine, coralbay, order, redeliveries
):
    session.rollback()
    truncate_all(engine)
    session.expunge_all()

    sequence = [_event(index) for index in order]
    sequence += [_event(index) for index in redeliveries]

    outcomes = [apply_delivery(session, coralbay, event).outcome for event in sequence]
    session.commit()

    booking = session.execute(select(Booking)).scalar_one()
    assert booking.status == NEWEST[1]
    assert booking.updated_at_raw == NEWEST[2]
    assert booking.unit_amount_cents == NEWEST[3]

    # Exactly four deliveries were ever allowed to change anything: one per
    # distinct event. Everything else was a duplicate or was refused as older.
    assert sum(1 for outcome in outcomes if outcome is DeliveryOutcome.APPLIED) <= len(TIMELINE)
    assert all(
        outcome in {DeliveryOutcome.APPLIED, DeliveryOutcome.DUPLICATE, DeliveryOutcome.SUPERSEDED}
        for outcome in outcomes
    )
