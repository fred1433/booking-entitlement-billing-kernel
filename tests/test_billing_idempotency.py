"""The billing run, and the promise it has to keep through a crash.

The promise is one sentence: the same entitlement is never charged twice for
the same period, whatever happens in the middle. These tests break the run in
the three places it can actually break, and check the promise each time.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from conftest import coralbay_event, lindhoff_row
from kernel.billing import (
    BillingRunResult,
    Charge,
    FakeStripe,
    IdempotencyConflict,
    StripePaymentProvider,
    charge_prepared,
    prepare,
    reconcile,
    render_reconciliation,
    run_billing,
    unblock_line,
)
from kernel.billing.run import idempotency_key
from kernel.entitlements import consume_claim_token, issue_claim_token
from kernel.intake import apply_delivery
from kernel.models import BillingLine, Entitlement, LineState, QuarantineItem

PERIOD = "2026-08"


class Crash(RuntimeError):
    pass


def billable(session, coralbay, settings, reference: str, amount: int = 24000) -> Entitlement:
    apply_delivery(session, coralbay, coralbay_event(
        event_id=f"evt-{reference}", booking_ref=reference, amount_minor=amount))
    session.commit()
    entitlement = session.execute(
        select(Entitlement).order_by(Entitlement.id.desc()).limit(1)
    ).scalar_one()
    token = issue_claim_token(session, entitlement, settings)
    consume_claim_token(session, token, f"holder-{reference}", settings)
    session.commit()
    return entitlement


def test_a_run_started_twice_still_produces_one_line_per_entitlement(session, coralbay, settings):
    """Two schedulers, one period. The unique index settles it, not a lock in code."""
    for index in range(3):
        billable(session, coralbay, settings, f"CB-10{index}")

    first = prepare(session, PERIOD)
    second = prepare(session, PERIOD)

    assert (first.prepared, first.already_prepared) == (3, 0)
    assert (second.prepared, second.already_prepared) == (0, 3)
    assert len(session.execute(select(BillingLine)).scalars().all()) == 3


def test_a_run_charges_each_line_exactly_once(session, coralbay, settings):
    for index in range(3):
        billable(session, coralbay, settings, f"CB-11{index}")
    provider = FakeStripe()

    run_billing(session, PERIOD, provider)
    run_billing(session, PERIOD, provider)

    assert provider.charges == 3
    assert provider.attempts == 3
    states = [line.state for line in session.execute(select(BillingLine)).scalars()]
    assert states == [LineState.CHARGED.value] * 3


def test_a_crash_in_the_middle_leaves_no_gap_and_no_second_charge(session, coralbay, settings):
    """The run dies after two lines. Starting it again finishes the job and nothing more."""
    for index in range(4):
        billable(session, coralbay, settings, f"CB-12{index}")
    provider = FakeStripe()
    charged: list[int] = []

    def die_after_two(line_id: int) -> None:
        charged.append(line_id)
        if len(charged) == 2:
            raise Crash("the worker was restarted")

    with pytest.raises(Crash):
        run_billing(session, PERIOD, provider, after_each=die_after_two)
    session.rollback()

    assert provider.charges == 2
    run_billing(session, PERIOD, provider)

    assert provider.charges == 4
    lines = session.execute(select(BillingLine).order_by(BillingLine.id)).scalars().all()
    assert [line.state for line in lines] == [LineState.CHARGED.value] * 4
    assert len({line.charge_ref for line in lines}) == 4


def test_a_charge_whose_answer_was_lost_is_not_taken_a_second_time(session, coralbay, settings):
    """The worst case, and the only one the idempotency key exists for.

    The provider takes the money and the process dies before it hears back. The
    line is still marked prepared, so the next run charges it again. It is safe
    only because the key is derived from the line rather than generated per
    attempt: the provider recognises its own earlier work and replays the
    answer instead of taking the money twice.
    """
    entitlement = billable(session, coralbay, settings, "CB-1300")
    lost_key = idempotency_key(entitlement.id, PERIOD)
    provider = FakeStripe(fail_before_response=lambda key: key == lost_key)

    result = prepare(session, PERIOD)
    charge_prepared(session, PERIOD, provider, result)
    session.commit()

    assert provider.charges == 1                 # the money moved
    line = session.execute(select(BillingLine)).scalar_one()
    assert line.state == LineState.PREPARED.value  # and we do not know it yet

    provider.fail_before_response = None         # the next run gets an answer
    charge_prepared(session, PERIOD, provider)
    session.commit()

    assert provider.attempts == 2
    assert provider.charges == 1                 # still exactly one charge
    line = session.execute(select(BillingLine)).scalar_one()
    assert line.state == LineState.CHARGED.value
    assert line.charge_ref == "ch_fake_000001"


def test_the_same_key_with_a_different_amount_is_an_error_not_a_second_charge(
    session, coralbay, settings
):
    """The key only protects a request that is itself derived.

    If the amount can move while the key stays put, the provider has no way to
    tell a retry from a new intention, and neither do we. It says so, and the
    line is blocked rather than guessed at.
    """
    entitlement = billable(session, coralbay, settings, "CB-1400")
    provider = FakeStripe()
    run_billing(session, PERIOD, provider)

    line = session.execute(select(BillingLine)).scalar_one()
    line.state = LineState.PREPARED.value
    line.amount_cents = line.amount_cents + 500          # somebody "fixed" the price
    session.commit()

    charge_prepared(session, PERIOD, provider)
    session.commit()

    assert provider.charges == 1
    line = session.execute(select(BillingLine)).scalar_one()
    assert line.state == LineState.BLOCKED.value
    assert "was first used for" in line.reason


def test_cancelling_before_the_charge_voids_the_line(session, coralbay, settings):
    billable(session, coralbay, settings, "CB-1500")
    prepare(session, PERIOD)

    apply_delivery(session, coralbay, coralbay_event(
        event_id="evt-cancel", booking_ref="CB-1500", state="cancelled",
        updated_at="2026-08-05T09:00:00+02:00"))
    session.commit()

    line = session.execute(select(BillingLine)).scalar_one()
    assert line.state == LineState.VOIDED.value

    provider = FakeStripe()
    charge_prepared(session, PERIOD, provider)
    assert provider.attempts == 0


def test_cancelling_after_the_charge_leaves_a_refund_somebody_has_to_decide_about(
    session, coralbay, settings
):
    """The line is not deleted. Deleting it would make the next run bill the period again."""
    billable(session, coralbay, settings, "CB-1600")
    provider = FakeStripe()
    run_billing(session, PERIOD, provider)

    apply_delivery(session, coralbay, coralbay_event(
        event_id="evt-cancel-2", booking_ref="CB-1600", state="cancelled",
        updated_at="2026-08-05T09:00:00+02:00"))
    session.commit()

    line = session.execute(select(BillingLine)).scalar_one()
    assert line.state == LineState.REFUND_DUE.value
    assert provider.charges == 1

    run_billing(session, PERIOD, provider)
    assert provider.charges == 1

    report = reconcile(session, PERIOD)
    assert report.count(LineState.REFUND_DUE.value) == 1
    assert report.refund_due_cents == 48000        # 24000 per person, two people


def test_an_entitlement_whose_source_data_is_in_quarantine_is_not_billed(
    session, coralbay, lindhoff, settings
):
    """Billing a booking we have an open question about is how a wrong invoice goes out.

    The line exists, so the period is on the record and a later run cannot bill
    it by accident. It is blocked, and the reconciliation says who we are
    waiting on.
    """
    apply_delivery(session, lindhoff, lindhoff_row(
        external_ref="LH-1700", changed_at="2026-08-01 09:00:00"), channel="file")
    session.commit()
    entitlement = session.execute(select(Entitlement)).scalar_one()
    token = issue_claim_token(session, entitlement, settings)
    consume_claim_token(session, token, "holder", settings)
    session.commit()

    # A second row with the same timestamp and a different price: a conflict.
    apply_delivery(session, lindhoff, lindhoff_row(
        external_ref="LH-1700", changed_at="2026-08-01 09:00:00", price_cents=99000),
        channel="file")
    session.commit()

    provider = FakeStripe()
    run_billing(session, PERIOD, provider)

    assert provider.attempts == 0
    line = session.execute(select(BillingLine)).scalar_one()
    assert line.state == LineState.BLOCKED.value
    report = reconcile(session, PERIOD)
    assert [item.kind for item in report.waiting_on_partners] == ["conflicting_versions"]


def test_a_blocked_line_is_charged_once_the_partner_has_answered(
    session, coralbay, lindhoff, settings
):
    apply_delivery(session, lindhoff, lindhoff_row(
        external_ref="LH-1800", changed_at="2026-08-01 09:00:00"), channel="file")
    session.commit()
    entitlement = session.execute(select(Entitlement)).scalar_one()
    consume_claim_token(session, issue_claim_token(session, entitlement, settings), "holder", settings)
    apply_delivery(session, lindhoff, lindhoff_row(
        external_ref="LH-1800", changed_at="2026-08-01 09:00:00", price_cents=99000),
        channel="file")
    session.commit()

    provider = FakeStripe()
    run_billing(session, PERIOD, provider)
    line = session.execute(select(BillingLine)).scalar_one()
    assert line.state == LineState.BLOCKED.value

    item = session.execute(select(QuarantineItem)).scalar_one()
    item.resolved_at = line.prepared_at
    unblock_line(session, line.id, "partner confirmed the 18000 version is current")
    session.commit()

    charge_prepared(session, PERIOD, provider)
    session.commit()
    assert provider.charges == 1
    assert session.execute(select(BillingLine)).scalar_one().state == LineState.CHARGED.value


def test_the_reconciliation_says_what_is_open_and_for_how_long(session, coralbay, lindhoff, settings):
    billable(session, coralbay, settings, "CB-1900")
    apply_delivery(session, lindhoff, lindhoff_row(
        external_ref="LH-1900", status="HOLD"), channel="file")
    session.commit()
    run_billing(session, PERIOD, FakeStripe())

    text_report = render_reconciliation(reconcile(session, PERIOD))

    assert "charged" in text_report
    assert "waiting on partners" in text_report
    assert "unknown_status_value" in text_report
    assert "EUR 480.00" in text_report


def test_the_payment_adapter_passes_the_derived_key_to_the_client(session):
    """Proof that the key reaches the call, with no key, no network, no library."""
    seen: list[tuple[dict, str]] = []

    def fake_client(params: dict, key: str) -> dict:
        seen.append((params, key))
        return {"id": "pi_recorded"}

    provider = StripePaymentProvider(create_payment_intent=fake_client)
    charge = provider.create_charge("bill:42:2026-08", 24000, "EUR", {"entitlement": "42"})

    assert isinstance(charge, Charge)
    assert seen[0][1] == "bill:42:2026-08"
    assert seen[0][0]["amount"] == 24000
    assert seen[0][0]["currency"] == "eur"
