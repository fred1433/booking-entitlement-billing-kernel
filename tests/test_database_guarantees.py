"""The guarantees that survive a change to the service layer.

Every assertion here goes around the kernel and writes raw SQL. That is the
point: application code is the part a future change can walk around by
accident, a unique index is not. If one of these tests goes green after a
migration that dropped an index, it would be lying, so each one asserts on the
name of the constraint that refused.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from conftest import coralbay_event
from kernel.billing import FakeStripe, run_billing
from kernel.entitlements import consume_claim_token, issue_claim_token
from kernel.intake import apply_delivery
from kernel.models import BillingLine, Entitlement, EntitlementShare


def _violates(session, statement: str, constraint: str) -> None:
    with pytest.raises(IntegrityError) as raised:
        session.execute(text(statement))
        session.flush()
    assert constraint in str(raised.value)
    session.rollback()


def test_one_booking_per_partner_reference(session, coralbay):
    apply_delivery(session, coralbay, coralbay_event(booking_ref="CB-2100"))
    session.commit()

    _violates(
        session,
        """
        insert into bookings (partner, external_id, status, customer_ref, product_code,
                              quantity, starts_at, ends_at, unit_amount_cents, currency,
                              updated_at_raw, updated_at_lo, updated_at_hi,
                              updated_at_ambiguous, payload_fingerprint)
        select partner, external_id, status, customer_ref, product_code, quantity,
               starts_at, ends_at, unit_amount_cents, currency, updated_at_raw,
               updated_at_lo, updated_at_hi, updated_at_ambiguous, payload_fingerprint
        from bookings limit 1
        """,
        "uq_booking_identity",
    )


def test_one_application_per_partner_event_id(session, coralbay):
    apply_delivery(session, coralbay, coralbay_event(event_id="evt-2200", booking_ref="CB-2200"))
    session.commit()

    _violates(
        session,
        """
        insert into processed_events (partner, source_event_id, first_delivery_id)
        values ('coralbay', 'evt-2200', 1)
        """,
        "uq_processed_event",
    )


def test_a_fourth_share_cannot_exist_even_in_raw_sql(session, coralbay, settings):
    apply_delivery(session, coralbay, coralbay_event(booking_ref="CB-2300"))
    session.commit()
    entitlement = session.execute(select(Entitlement)).scalar_one()
    consume_claim_token(session, issue_claim_token(session, entitlement, settings), "holder", settings)
    session.execute(text(
        "insert into entitlement_shares (entitlement_id, share_index, shared_with) "
        f"values ({entitlement.id}, 1, 'a'), ({entitlement.id}, 2, 'b'), ({entitlement.id}, 3, 'c')"
    ))
    session.commit()

    _violates(
        session,
        f"insert into entitlement_shares (entitlement_id, share_index, shared_with) "
        f"values ({entitlement.id}, 4, 'd')",
        "ck_share_index_within_limit",
    )
    _violates(
        session,
        f"insert into entitlement_shares (entitlement_id, share_index, shared_with) "
        f"values ({entitlement.id}, 3, 'd')",
        "uq_share_slot",
    )
    assert len(session.execute(select(EntitlementShare)).scalars().all()) == 3


def test_a_second_line_for_the_same_entitlement_and_period_cannot_exist(
    session, coralbay, settings
):
    """The promise that matters most, kept by the schema rather than by the run."""
    apply_delivery(session, coralbay, coralbay_event(booking_ref="CB-2400"))
    session.commit()
    entitlement = session.execute(select(Entitlement)).scalar_one()
    consume_claim_token(session, issue_claim_token(session, entitlement, settings), "holder", settings)
    session.commit()
    run_billing(session, "2026-08", FakeStripe())

    line = session.execute(select(BillingLine)).scalar_one()
    _violates(
        session,
        f"""
        insert into billing_lines (run_id, entitlement_id, period, amount_cents, currency,
                                   idempotency_key, state)
        values ({line.run_id}, {line.entitlement_id}, '{line.period}', {line.amount_cents},
                '{line.currency}', 'some-other-key', 'prepared')
        """,
        "uq_line_per_entitlement_period",
    )
    _violates(
        session,
        f"""
        insert into billing_lines (run_id, entitlement_id, period, amount_cents, currency,
                                   idempotency_key, state)
        values ({line.run_id}, {line.entitlement_id}, '2026-09', {line.amount_cents},
                '{line.currency}', '{line.idempotency_key}', 'prepared')
        """,
        "uq_line_idempotency_key",
    )


def test_a_claim_token_hash_is_unique(session, coralbay, settings):
    apply_delivery(session, coralbay, coralbay_event(booking_ref="CB-2500"))
    session.commit()
    entitlement = session.execute(select(Entitlement)).scalar_one()
    issue_claim_token(session, entitlement, settings)
    session.commit()

    _violates(
        session,
        """
        insert into claim_tokens (entitlement_id, token_hash, expires_at)
        select entitlement_id, token_hash, expires_at from claim_tokens limit 1
        """,
        "claim_tokens_token_hash_key",
    )


def test_an_entitlement_state_outside_the_lifecycle_cannot_be_written(session, coralbay):
    apply_delivery(session, coralbay, coralbay_event(booking_ref="CB-2600"))
    session.commit()

    _violates(
        session,
        "update entitlements set state = 'refunded'",
        "ck_entitlement_state",
    )
