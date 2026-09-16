"""The guarantees the database keeps, tested by trying to break them in raw SQL.

Some of what this sample promises is kept by unique indexes and check
constraints rather than by service code. That is deliberate: service code is
the part a future change can walk around by accident, and a unique index is
not.

Each test below goes around the service layer entirely, writes the forbidden
row by hand, and asserts on the **name of the constraint that refused**. A test
that only asserts "an error was raised" passes when the error is a typo.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from conftest import PERIOD, coralbay_event
from kernel import intake
from kernel.billing import run
from kernel.billing.payments import request_fingerprint


def _insert_booking(session, external_id: str) -> int:
    return session.execute(
        text(
            "insert into bookings (partner, external_id, status, customer_ref, product_code, "
            "quantity, starts_at, ends_at, amount_cents, amount_basis, currency, updated_at_raw, "
            "updated_at_lo, updated_at_hi, updated_at_ambiguous, payload_fingerprint, "
            "first_seen_at, last_applied_at) "
            "values ('coralbay', :ext, 'confirmed', 'c', 'p', 1, now(), now(), 1000, 'per_unit', "
            "'EUR', 'x', now(), now(), false, 'fp', now(), now()) returning id"
        ),
        {"ext": external_id},
    ).scalar_one()


def test_one_booking_per_partner_reference(session):
    """Two rows claiming to be the same booking cannot coexist."""
    _insert_booking(session, "CB-DB-1")
    session.flush()
    with pytest.raises(IntegrityError) as raised:
        _insert_booking(session, "CB-DB-1")
        session.flush()
    assert "uq_booking_identity" in str(raised.value)


def test_one_application_per_partner_event_id(session):
    """The at-least-once control is an index, so two workers cannot both win."""
    session.execute(
        text(
            "insert into processed_events (partner, source_event_id, first_delivery_id, processed_at) "
            "values ('coralbay', 'evt-db', 1, now())"
        )
    )
    session.flush()
    with pytest.raises(IntegrityError) as raised:
        session.execute(
            text(
                "insert into processed_events (partner, source_event_id, first_delivery_id, processed_at) "
                "values ('coralbay', 'evt-db', 2, now())"
            )
        )
        session.flush()
    assert "uq_processed_event" in str(raised.value)


def test_a_second_line_for_the_same_entitlement_and_period_cannot_exist(session, coralbay):
    """Even with the service layer out of the way, a period is billed once."""
    intake.apply_delivery(session, coralbay, coralbay_event(event_id="e1", booking_ref="CB-DB-2"))
    run.prepare(session, PERIOD)
    session.flush()
    entitlement_id = session.execute(text("select id from entitlements limit 1")).scalar_one()

    with pytest.raises(IntegrityError) as raised:
        session.execute(
            text(
                "insert into billing_lines (run_id, entitlement_id, period, amount_cents, currency, "
                "idempotency_key, request_fingerprint, state, cancellation, prepared_at) "
                "values ((select id from billing_runs limit 1), :ent, :period, 100, 'EUR', "
                "'other-key', 'fp', 'prepared', 'none', now())"
            ),
            {"ent": entitlement_id, "period": PERIOD},
        )
        session.flush()
    assert "uq_line_per_entitlement_period" in str(raised.value)


def test_two_lines_cannot_share_an_idempotency_key(session, coralbay):
    """The key names one intent. Two intents under one key is the double charge."""
    intake.apply_delivery(session, coralbay, coralbay_event(event_id="e1", booking_ref="CB-DB-3"))
    run.prepare(session, PERIOD)
    session.flush()
    key = session.execute(text("select idempotency_key from billing_lines limit 1")).scalar_one()

    booking_id = _insert_booking(session, "CB-DB-4")
    entitlement_id = session.execute(
        text(
            "insert into entitlements (booking_id, state, created_at, billable_from, billable_to) "
            "values (:b, 'active', now(), now(), now()) returning id"
        ),
        {"b": booking_id},
    ).scalar_one()

    with pytest.raises(IntegrityError) as raised:
        session.execute(
            text(
                "insert into billing_lines (run_id, entitlement_id, period, amount_cents, currency, "
                "idempotency_key, request_fingerprint, state, cancellation, prepared_at) "
                "values ((select id from billing_runs limit 1), :ent, :period, 100, 'EUR', "
                ":key, 'fp', 'prepared', 'none', now())"
            ),
            {"ent": entitlement_id, "period": PERIOD, "key": key},
        )
        session.flush()
    assert "uq_line_idempotency_key" in str(raised.value)


def test_the_simulated_provider_cannot_hold_two_operations_for_one_key(session):
    """The simulation is held to the contract it claims to reproduce."""
    session.execute(
        text(
            "insert into simulated_provider_charges (idempotency_key, request_fingerprint, "
            "charge_ref, amount_cents, currency, created_at) "
            "values ('k1', :fp, 'sim_1', 100, 'EUR', now())"
        ),
        {"fp": request_fingerprint(100, "EUR")},
    )
    session.flush()
    with pytest.raises(IntegrityError) as raised:
        session.execute(
            text(
                "insert into simulated_provider_charges (idempotency_key, request_fingerprint, "
                "charge_ref, amount_cents, currency, created_at) "
                "values ('k1', :fp, 'sim_2', 100, 'EUR', now())"
            ),
            {"fp": request_fingerprint(100, "EUR")},
        )
        session.flush()
    assert "uq_simulated_charge_key" in str(raised.value)


def test_an_obligation_state_outside_the_two_cannot_be_written(session):
    """The vocabulary is enforced where the data is, not only where it is parsed."""
    booking_id = _insert_booking(session, "CB-DB-5")
    session.flush()
    with pytest.raises(IntegrityError) as raised:
        session.execute(
            text(
                "insert into entitlements (booking_id, state, created_at) "
                "values (:b, 'claimable', now())"
            ),
            {"b": booking_id},
        )
        session.flush()
    assert "ck_entitlement_state" in str(raised.value)
