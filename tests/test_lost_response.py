"""The external operation succeeded and the answer never came back.

This is the case the whole sample exists for, so it is staged rather than
asserted. The stand-in provider commits its effect to its own table on its own
connection **before** the caller hears anything, and then the answer is
dropped. That ordering is what makes the situation real: at the moment the
worker dies, the operation has happened and nothing in the worker knows it.

Four things are shown here, and the first one matters as much as the rest:

* the nominal path, so that "no duplicates" cannot be achieved by nothing ever
  working;
* the lost answer, and the durable intent it leaves behind;
* the resume inside the retention window, which replays rather than repeats;
* the resume outside it, which refuses to send, and the manual step that closes
  the line by establishing the outcome instead of retrying it.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from conftest import PERIOD, coralbay_event
from kernel import intake
from kernel.billing import payments, run
from kernel.models import BillingLine, LineState


def _one_booking(session, coralbay, ref="CB-2001", amount=24000, pax=2):
    intake.apply_delivery(
        session, coralbay,
        coralbay_event(event_id=f"evt-{ref}", booking_ref=ref, amount_minor=amount, pax=pax),
    )
    session.commit()


# --------------------------------------------------------------------------- #

def test_the_nominal_path_actually_charges(session, session_factory, coralbay, provider):
    """The control. Without it, every assertion below is satisfied by doing nothing."""
    _one_booking(session, coralbay)
    run.prepare(session, PERIOD)
    session.commit()

    summary = run.submit(session_factory, provider, PERIOD)

    assert summary["charged"] == 1
    assert provider.total_operations() == 1
    with session_factory() as fresh:
        assert fresh.scalars(select(BillingLine)).one().state == LineState.CHARGED.value


def test_a_lost_answer_leaves_an_intent_that_survives_the_worker(
    session, session_factory, coralbay, provider
):
    """The worker dies after the operation happened and before it was told.

    What is left behind is a committed row that names the key and the frozen
    parameters. Without that row there is no safe way back: the next run would
    have to choose between never charging and charging again.
    """
    _one_booking(session, coralbay)
    run.prepare(session, PERIOD)
    session.commit()

    provider.lose_response_for = lambda key: True
    summary = run.submit(session_factory, provider, PERIOD)

    assert summary["lost"] == 1
    assert provider.total_operations() == 1, "the operation did happen"

    # A different session stands in for a restarted process: nothing is carried
    # over in memory, only what was committed.
    with session_factory() as fresh:
        line = fresh.scalars(select(BillingLine)).one()
        assert line.state == LineState.SUBMITTED.value
        assert line.submitted_at is not None
        assert line.idempotency_key == "bill:1:2026-08"
        assert line.request_fingerprint is not None
        assert line.charge_ref is None


def test_the_resume_replays_rather_than_repeats(session, session_factory, coralbay, provider):
    """Inside the window, the same key with the same parameters is presented again.

    The provider answers with the operation it already carried out. The count of
    real operations does not move, and the line is settled on that answer.
    """
    _one_booking(session, coralbay)
    run.prepare(session, PERIOD)
    session.commit()
    provider.lose_response_for = lambda key: True
    run.submit(session_factory, provider, PERIOD)
    assert provider.total_operations() == 1

    provider.lose_response_for = None
    summary = run.resume(session_factory, provider, PERIOD)

    assert summary["replayed"] == 1
    assert provider.total_operations() == 1, "still one operation, not two"
    with session_factory() as fresh:
        line = fresh.scalars(select(BillingLine)).one()
        assert line.state == LineState.CHARGED.value
        assert line.charge_ref == "sim_000001"


def test_outside_the_retention_window_nothing_is_sent(
    session, session_factory, coralbay, provider
):
    """The key has been forgotten, so presenting it again would create a new request.

    This is the branch that usually does not exist. The line is marked
    unresolved and stays on the report; it is not retried, and it is not quietly
    written off either.
    """
    _one_booking(session, coralbay)
    run.prepare(session, PERIOD)
    session.commit()
    provider.lose_response_for = lambda key: True
    run.submit(session_factory, provider, PERIOD)

    provider.lose_response_for = None
    attempts_before = provider.attempts
    with session_factory() as fresh:
        line = fresh.scalars(select(BillingLine)).one()
        late = line.submitted_at + timedelta(hours=30)

    summary = run.resume(session_factory, provider, PERIOD, now=late)

    assert summary["unresolved"] == 1
    assert provider.attempts == attempts_before, "the provider was not called at all"
    assert provider.total_operations() == 1
    with session_factory() as fresh:
        assert fresh.scalars(select(BillingLine)).one().state == LineState.UNRESOLVED.value


def test_an_unresolved_line_is_closed_by_looking_it_up_not_by_retrying(
    session, session_factory, coralbay, provider
):
    """The manual step, in the repository because the alternative is the bug.

    Establishing the outcome against the provider's own records is what turns
    an unresolved line into a settled one without a second operation.
    """
    _one_booking(session, coralbay)
    run.prepare(session, PERIOD)
    session.commit()
    provider.lose_response_for = lambda key: True
    run.submit(session_factory, provider, PERIOD)
    with session_factory() as fresh:
        line = fresh.scalars(select(BillingLine)).one()
        late = line.submitted_at + timedelta(hours=30)
        line_id = line.id
    provider.lose_response_for = None
    run.resume(session_factory, provider, PERIOD, now=late)

    with session_factory() as fresh:
        verdict = run.establish_out_of_band(fresh, provider, line_id)
        fresh.commit()

    assert verdict == "already_happened"
    assert provider.total_operations() == 1
    with session_factory() as fresh:
        line = fresh.scalars(select(BillingLine)).one()
        assert line.state == LineState.CHARGED.value
        assert line.charge_ref == "sim_000001"


def test_the_same_key_with_different_parameters_is_an_error_not_a_second_operation(
    session, session_factory, coralbay, provider
):
    """A key that stays put while the amount moves protects nothing.

    The provider refuses, the way the documented behaviour says it does, rather
    than carrying out a second operation or silently returning the first.
    """
    _one_booking(session, coralbay)
    run.prepare(session, PERIOD)
    session.commit()
    run.submit(session_factory, provider, PERIOD)

    with session_factory() as fresh:
        line = fresh.scalars(select(BillingLine)).one()
        key = line.idempotency_key

    with pytest.raises(payments.IdempotencyConflict):
        provider.create_charge(key, payments.request_fingerprint(99999, "EUR"), 99999, "EUR")

    assert provider.total_operations() == 1


def test_the_frozen_parameters_and_derived_key_reach_the_call():
    """What a real client would receive, without a key, a network or the library."""
    seen: dict = {}

    def create(body, idempotency_key):
        seen.update({"body": body, "key": idempotency_key})
        return {"id": "ext_1"}

    client = payments.InjectedClientProvider(create=create)
    key = run.idempotency_key_for(7, PERIOD)
    client.create_charge(key, payments.request_fingerprint(1500, "EUR"), 1500, "EUR")

    assert seen["key"] == "bill:7:2026-08"
    assert seen["body"]["amount"] == 1500
    assert seen["body"]["currency"] == "eur"
