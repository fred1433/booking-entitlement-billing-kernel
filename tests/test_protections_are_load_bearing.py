"""Every protection, removed on purpose, to show the suite notices.

A green suite is not evidence on its own. A test that would still pass with the
thing it claims to test taken out is decoration, and it is easy to write by
accident: assert that no duplicate charge happened, in a scenario where nothing
was ever charged.

So each control has a switch in ``kernel.mutations``, and each test below turns
one off and asserts that the dangerous outcome then happens. The same scenario
with the switch on is asserted next to it, in the same test, so the two lines
can be read together.
"""

from __future__ import annotations

from sqlalchemy import select

from conftest import PERIOD, coralbay_event
from kernel import intake, mutations
from kernel.billing import run
from kernel.models import BillingLine, Booking, DeliveryOutcome, LineState


def _once():
    """Lose exactly the first answer.

    The provider commits its effect before it decides whether to answer, so a
    predicate that looks at how many operations have happened is already too
    late to see zero.
    """
    seen: list[str] = []

    def lose(key: str) -> bool:
        seen.append(key)
        return len(seen) == 1

    return lose


def _booking(session, coralbay, ref):
    intake.apply_delivery(session, coralbay, coralbay_event(event_id=f"e-{ref}", booking_ref=ref))
    session.commit()


def test_without_a_derived_key_a_lost_answer_becomes_a_second_operation(
    session, session_factory, coralbay, provider
):
    """The headline claim, shown to be load bearing.

    With the key derived from the line, the retry replays. With a fresh key per
    attempt, the same retry is a new request, and the money moves twice.
    """
    _booking(session, coralbay, "CB-7001")

    with mutations.without(mutations.DERIVED_IDEMPOTENCY_KEY):
        run.prepare(session, PERIOD)
        session.commit()
        provider.lose_response_for = _once()
        run.submit(session_factory, provider, PERIOD)
        # A fresh key on the retry: the provider has never seen it before.
        with session_factory() as fresh:
            line = fresh.scalars(select(BillingLine)).one()
            line.idempotency_key = line.idempotency_key + "-retry"
            fresh.commit()
        provider.lose_response_for = None
        run.resume(session_factory, provider, PERIOD)

    assert provider.total_operations() == 2, "the protection removed, the money moved twice"


def test_with_the_derived_key_the_same_scenario_moves_money_once(
    session, session_factory, coralbay, provider
):
    """The same scenario, nothing removed. This is the line the one above is compared to."""
    _booking(session, coralbay, "CB-7002")
    run.prepare(session, PERIOD)
    session.commit()
    provider.lose_response_for = _once()
    run.submit(session_factory, provider, PERIOD)
    provider.lose_response_for = None
    run.resume(session_factory, provider, PERIOD)

    assert provider.total_operations() == 1


def test_without_a_committed_intent_a_crash_leaves_nothing_to_resume_from(
    session, session_factory, coralbay, provider
):
    """Recording the attempt after the call is the same as not recording it.

    The operation happens, the process loses the answer, and the line is still
    sitting in ``prepared``: nothing says it was ever attempted, so the next run
    attempts it again.
    """
    _booking(session, coralbay, "CB-7003")
    run.prepare(session, PERIOD)
    session.commit()

    with mutations.without(mutations.INTENT_BEFORE_CALL):
        provider.lose_response_for = lambda key: True
        run.submit(session_factory, provider, PERIOD)

    assert provider.total_operations() == 1
    with session_factory() as fresh:
        line = fresh.scalars(select(BillingLine)).one()
        assert line.state == LineState.PREPARED.value, "no trace that anything was attempted"
        assert line.submitted_at is None


def test_without_the_hold_an_undecidable_booking_is_billed_anyway(session, coralbay):
    """Billing on data nobody could interpret, which is the quiet version of the bug."""
    stamp = "2026-08-04T09:15:00+02:00"
    intake.apply_delivery(session, coralbay, coralbay_event(
        event_id="e1", booking_ref="CB-7004", updated_at=stamp, pax=2))
    intake.apply_delivery(session, coralbay, coralbay_event(
        event_id="e2", booking_ref="CB-7004", updated_at=stamp, pax=5))
    session.commit()

    with mutations.without(mutations.UNDECIDED_BLOCKS_BILLING):
        run.prepare(session, PERIOD)
        session.commit()

    line = session.scalars(select(BillingLine)).one()
    assert line.state == LineState.PREPARED.value, "prepared while the question is open"


def test_without_the_conflict_rule_one_version_disappears_silently(session, coralbay):
    """Last write wins, and nothing in the data afterwards says an update was dropped."""
    stamp = "2026-08-04T09:15:00+02:00"
    intake.apply_delivery(session, coralbay, coralbay_event(
        event_id="e1", booking_ref="CB-7005", updated_at=stamp, pax=2))

    with mutations.without(mutations.CONFLICT_IS_NOT_A_DUPLICATE):
        result = intake.apply_delivery(session, coralbay, coralbay_event(
            event_id="e2", booking_ref="CB-7005", updated_at=stamp, pax=5))
        session.commit()

    assert result.outcome is DeliveryOutcome.APPLIED
    assert session.scalars(select(Booking)).one().quantity == 5, "the other version is gone"
