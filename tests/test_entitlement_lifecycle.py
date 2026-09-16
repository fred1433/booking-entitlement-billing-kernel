"""The entitlement lifecycle, and the transitions it refuses."""

from __future__ import annotations

import threading

import pytest
from sqlalchemy import select, text

from conftest import coralbay_event, wait_until_blocked
from kernel.entitlements import (
    ClaimRefused,
    IllegalTransition,
    ShareLimitReached,
    activate,
    consume_claim_token,
    expire_due,
    issue_claim_token,
    share,
    transition,
)
from kernel.clock import utc_now
from kernel.intake import apply_delivery
from kernel.models import Entitlement, EntitlementShare, EntitlementState, JournalEntry


def _entitlement(session, coralbay, reference="CB-1001", **event):
    apply_delivery(session, coralbay, coralbay_event(booking_ref=reference, **event))
    session.commit()
    return session.execute(select(Entitlement)).scalar_one()


def test_a_confirmed_booking_produces_a_claimable_entitlement(session, coralbay):
    entitlement = _entitlement(session, coralbay)
    assert entitlement.state == EntitlementState.CLAIMABLE.value


def test_a_booking_that_arrives_already_cancelled_creates_nothing(session, coralbay):
    """The first thing we ever hear about a booking can be its cancellation."""
    apply_delivery(session, coralbay, coralbay_event(booking_ref="CB-1002", state="cancelled"))
    session.commit()
    assert session.execute(select(Entitlement)).scalars().all() == []


def test_a_transition_outside_the_table_is_refused_and_written_down(session, coralbay):
    """A refusal nobody can read is indistinguishable from a bug."""
    entitlement = _entitlement(session, coralbay)

    with pytest.raises(IllegalTransition):
        transition(session, entitlement, EntitlementState.ACTIVE.value)
    session.commit()

    refusal = session.execute(
        select(JournalEntry).where(JournalEntry.action == "entitlement.transition",
                                   JournalEntry.outcome == "refused")
    ).scalar_one()
    assert refusal.reason == "claimable cannot become active"
    assert entitlement.state == EntitlementState.CLAIMABLE.value


def test_a_cancelled_entitlement_is_never_reopened_by_a_later_amendment(session, coralbay):
    """A cancelled booking that comes back is a new booking. Saying so is cheaper than an invoice."""
    apply_delivery(session, coralbay, coralbay_event(
        event_id="evt-1", booking_ref="CB-1003", updated_at="2026-08-04T09:00:00+02:00"))
    apply_delivery(session, coralbay, coralbay_event(
        event_id="evt-2", booking_ref="CB-1003", state="cancelled",
        updated_at="2026-08-04T10:00:00+02:00"))
    apply_delivery(session, coralbay, coralbay_event(
        event_id="evt-3", booking_ref="CB-1003", state="amended",
        updated_at="2026-08-04T11:00:00+02:00"))
    session.commit()

    entitlement = session.execute(select(Entitlement)).scalar_one()
    assert entitlement.state == EntitlementState.CANCELLED.value


def test_a_claim_link_is_spent_exactly_once(session, coralbay, settings):
    entitlement = _entitlement(session, coralbay)
    token = issue_claim_token(session, entitlement, settings)
    session.commit()

    claimed = consume_claim_token(session, token, "holder-a", settings)
    session.commit()
    assert claimed.state == EntitlementState.CLAIMED.value

    with pytest.raises(ClaimRefused):
        consume_claim_token(session, token, "someone-else", settings)


def test_a_claim_link_that_expired_is_refused(session, coralbay, settings):
    entitlement = _entitlement(session, coralbay)
    token = issue_claim_token(session, entitlement, settings, ttl_seconds=-1)
    session.commit()

    with pytest.raises(ClaimRefused):
        consume_claim_token(session, token, "holder", settings)


def test_a_forged_claim_link_is_refused_without_touching_the_database(session, coralbay, settings):
    """The signature is what makes a scanner walking the URL space cost us nothing."""
    entitlement = _entitlement(session, coralbay)
    token = issue_claim_token(session, entitlement, settings)
    session.commit()
    forged = token[:-1] + ("a" if token[-1] != "a" else "b")

    with pytest.raises(ClaimRefused, match="signature"):
        consume_claim_token(session, forged, "attacker", settings)


def test_the_kernel_refuses_to_issue_links_with_the_development_secret(session, coralbay):
    """A placeholder secret that reaches production signs every link with a public value."""
    entitlement = _entitlement(session, coralbay)
    with pytest.raises(RuntimeError, match="development placeholder"):
        issue_claim_token(session, entitlement)


def test_two_people_opening_the_same_claim_link_at_once_do_not_both_get_in(
    session, other_session, engine, coralbay, settings
):
    """The check and the consumption are one statement, so the race has one winner.

    A read then a write lets both requests see an unspent token. This test runs
    two real connections against one row and proves the second one leaves empty
    handed after the first commits.
    """
    entitlement = _entitlement(session, coralbay)
    token = issue_claim_token(session, entitlement, settings)
    session.commit()

    outcome: list[str] = []

    def second_person() -> None:
        try:
            consume_claim_token(other_session, token, "second", settings)
            other_session.commit()
            outcome.append("let in")
        except ClaimRefused:
            other_session.rollback()
            outcome.append("refused")

    consume_claim_token(session, token, "first", settings)   # holds the row lock
    racer = threading.Thread(target=second_person)
    racer.start()
    wait_until_blocked(engine)
    session.commit()
    racer.join(timeout=15)

    assert outcome == ["refused"]
    assert session.execute(text("select consumed_by from claim_tokens")).scalar_one() == "first"


def test_sharing_stops_at_the_bound(session, coralbay, settings):
    entitlement = _entitlement(session, coralbay)
    consume_claim_token(session, issue_claim_token(session, entitlement, settings), "holder", settings)
    for index in range(3):
        share(session, entitlement, f"friend-{index}")
    session.commit()

    with pytest.raises(ShareLimitReached):
        share(session, entitlement, "one-too-many")
    session.rollback()

    assert len(session.execute(select(EntitlementShare)).scalars().all()) == 3


def test_expiry_moves_only_what_is_due(session, coralbay, settings):
    entitlement = _entitlement(session, coralbay)
    entitlement.expires_at = utc_now().replace(year=2020)
    session.commit()

    expired = expire_due(session)
    session.commit()

    assert [item.id for item in expired] == [entitlement.id]
    assert session.get(Entitlement, entitlement.id).state == EntitlementState.EXPIRED.value


def test_activation_requires_a_claim_first(session, coralbay, settings):
    entitlement = _entitlement(session, coralbay)
    with pytest.raises(IllegalTransition):
        activate(session, entitlement)
