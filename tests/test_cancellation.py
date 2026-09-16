"""A cancellation lands in one of three places, and they are not the same place.

The instinct in all three is to delete the line. Then the next run finds nothing
for the period and does the whole thing again. So nothing is deleted here, and
what is recorded is which of the three situations it was, because only one of
them can be settled without asking anybody.

No refund rule is invented. What is owed after money has moved is a commercial
question, and a sample that answered it would be making something up.
"""

from __future__ import annotations

from sqlalchemy import select

from conftest import PERIOD, coralbay_event
from kernel import intake
from kernel.billing import run
from kernel.models import BillingLine, Cancellation, LineState


def _prepared(session, coralbay, ref):
    intake.apply_delivery(session, coralbay, coralbay_event(event_id=f"e-{ref}", booking_ref=ref))
    run.prepare(session, PERIOD)
    session.commit()
    return session.scalars(select(BillingLine)).one()


def test_cancelled_before_anything_was_submitted_is_simply_voided(session, coralbay):
    line = _prepared(session, coralbay, "CB-5001")

    run.cancel(session, line.entitlement_id, "the partner cancelled")
    session.commit()

    line = session.scalars(select(BillingLine)).one()
    assert line.state == LineState.VOIDED.value
    assert line.cancellation == Cancellation.BEFORE_SUBMISSION.value


def test_cancelled_while_the_outcome_is_unknown_cannot_be_decided_at_all(
    session, session_factory, coralbay, provider
):
    """Deciding requires knowing whether money moved, and that is the one unknown.

    The line is not voided, because voiding it would be a guess that nothing
    happened. It is not treated as charged either. It stays, marked, until the
    outcome is established.
    """
    line = _prepared(session, coralbay, "CB-5002")
    provider.lose_response_for = lambda key: True
    run.submit(session_factory, provider, PERIOD)

    with session_factory() as fresh:
        run.cancel(fresh, line.entitlement_id, "the partner cancelled")
        fresh.commit()

    with session_factory() as fresh:
        line = fresh.scalars(select(BillingLine)).one()
        assert line.state == LineState.SUBMITTED.value, "not voided, and not charged"
        assert line.cancellation == Cancellation.WHILE_UNRESOLVED.value
        assert "until it is established whether the operation happened" in line.reason


def test_cancelled_after_the_operation_leaves_a_decision_for_a_person(
    session, session_factory, coralbay, provider
):
    """The line stays and says what happened. It does not invent a refund."""
    line = _prepared(session, coralbay, "CB-5003")
    run.submit(session_factory, provider, PERIOD)

    with session_factory() as fresh:
        run.cancel(fresh, line.entitlement_id, "the partner cancelled")
        fresh.commit()

    with session_factory() as fresh:
        line = fresh.scalars(select(BillingLine)).one()
        assert line.state == LineState.CHARGED.value
        assert line.cancellation == Cancellation.AFTER_CHARGE.value
        assert "commercial decision" in line.reason
    assert provider.total_operations() == 1


def test_a_cancelled_obligation_is_not_billed_again_by_the_next_run(
    session, session_factory, coralbay, provider
):
    """The reason lines are never deleted, stated as a test."""
    line = _prepared(session, coralbay, "CB-5004")
    run.submit(session_factory, provider, PERIOD)
    with session_factory() as fresh:
        run.cancel(fresh, line.entitlement_id, "the partner cancelled")
        fresh.commit()

    with session_factory() as fresh:
        run.prepare(fresh, PERIOD)
        fresh.commit()

    with session_factory() as fresh:
        assert len(fresh.scalars(select(BillingLine)).all()) == 1
    assert provider.total_operations() == 1
