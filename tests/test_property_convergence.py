"""Two properties, and the difference between them is the interesting part.

The obvious property to write here is "any order of deliveries reaches the same
state". It is false, and writing it down was the useful part of the exercise:
with the conflict rule in place, two versions carrying the same timestamp and
different content leave the booking in whichever of them arrived **first**, so
the two orders end in different states on purpose.

So there are two properties instead:

* **convergence**, under a precondition that is stated rather than assumed: if
  the deliveries are individually orderable and no two of them collide, then
  permutation and redelivery do not change where the booking ends up;
* **safety under collision**, which is not convergence at all: whichever way a
  collision is ordered, the booking ends up undecidable for billing and the
  conflict is still on record. The two orders differ in what is held; they do
  not differ in whether somebody is told.

A third thing worth separating: state converging is not the same as effects
already produced converging. Money that has moved has moved, and no amount of
reordering deliveries takes it back. That is what the billing tests are for.
"""

from __future__ import annotations

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import select

from conftest import coralbay_event
from kernel import intake
from kernel.models import Booking, VersionConflict

SETTINGS = settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)

# Four distinct, individually orderable versions of one booking. Distinct
# timestamps is the precondition, and it is in the data rather than in a
# comment.
VERSIONS = [
    ("evt-a", "2026-08-01T09:00:00+02:00", 1, 10000),
    ("evt-b", "2026-08-02T09:00:00+02:00", 2, 20000),
    ("evt-c", "2026-08-03T09:00:00+02:00", 3, 30000),
    ("evt-d", "2026-08-04T09:00:00+02:00", 4, 40000),
]
NEWEST = VERSIONS[-1]


@SETTINGS
@given(
    order=st.permutations(range(len(VERSIONS))),
    repeats=st.lists(st.integers(min_value=0, max_value=len(VERSIONS) - 1),
                     min_size=0, max_size=6),
)
def test_orderable_deliveries_converge_whatever_the_order_and_the_repeats(
    engine, session_factory, coralbay, order, repeats
):
    """The precondition is explicit: every version here is orderable against every other.

    Under it, the booking ends on the newest version the partner sent, no matter
    which order the deliveries arrive in and no matter how many times any of
    them is delivered again. Overlapping pull windows are safe for exactly this
    reason.
    """
    from conftest import truncate_all

    truncate_all(engine)
    sequence = list(order) + list(repeats)

    with session_factory() as session:
        for index in sequence:
            event_id, stamp, pax, amount = VERSIONS[index]
            intake.apply_delivery(session, coralbay, coralbay_event(
                event_id=event_id, booking_ref="CB-PROP",
                updated_at=stamp, pax=pax, amount_minor=amount))
        session.commit()

    with session_factory() as session:
        booking = session.scalars(select(Booking)).one()
        assert booking.quantity == NEWEST[2]
        assert booking.amount_cents == NEWEST[3]


@SETTINGS
@given(first_is_a=st.booleans(), repeats=st.integers(min_value=0, max_value=3))
def test_a_collision_is_safe_in_either_order_even_though_it_does_not_converge(
    engine, session_factory, coralbay, first_is_a, repeats
):
    """Two versions, one timestamp, different content.

    The end state depends on which arrived first, and that is the rule working,
    not a bug: neither payload says which one is current, so the one being held
    is kept and nothing is overwritten. What does not depend on the order is the
    part that matters: the booking is undecidable for billing either way, and
    the conflict is on record either way.
    """
    from conftest import truncate_all

    truncate_all(engine)
    stamp = "2026-08-04T09:15:00+02:00"
    a = coralbay_event(event_id="evt-a", booking_ref="CB-COLLIDE", updated_at=stamp, pax=2)
    b = coralbay_event(event_id="evt-b", booking_ref="CB-COLLIDE", updated_at=stamp, pax=7)
    first, second = (a, b) if first_is_a else (b, a)

    with session_factory() as session:
        intake.apply_delivery(session, coralbay, first)
        for _ in range(repeats + 1):
            intake.apply_delivery(session, coralbay, second)
        session.commit()

    with session_factory() as session:
        booking = session.scalars(select(Booking)).one()
        # Order dependent, and correctly so: the first one seen is held.
        assert booking.quantity == (2 if first_is_a else 7)
        # Order independent, and this is the invariant worth having.
        assert booking.undecided_reason == "conflicting_versions"
        conflicts = session.scalars(
            select(VersionConflict).where(VersionConflict.resolved_at.is_(None))
        ).all()
        assert conflicts, "the collision is still on record"
