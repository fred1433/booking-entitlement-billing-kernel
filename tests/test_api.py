"""The HTTP surface, and why each status code is the one it is.

The status code a partner receives is a decision, not a formality. It is what
their retry policy reads, and getting it wrong turns a correct client into a
storm or into silent data loss.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from conftest import PERIOD, coralbay_event, lindhoff_row
from kernel.api.app import create_app


@pytest.fixture
def client(session_factory):
    return TestClient(create_app(session_factory))


def test_a_new_delivery_is_applied(client):
    response = client.post(
        "/partners/coralbay/deliveries",
        json=coralbay_event(event_id="api-1", booking_ref="CB-API-1"),
    )
    assert response.status_code == 200
    assert response.json()["outcome"] == "applied"


def test_a_repeat_is_two_hundred_not_an_error(client):
    """A partner retrying must be told "I have this", not "try again".

    Anything in the 4xx or 5xx range here turns a correct retry policy on their
    side into a storm on ours.
    """
    event = coralbay_event(event_id="api-2", booking_ref="CB-API-2")
    client.post("/partners/coralbay/deliveries", json=event)
    response = client.post("/partners/coralbay/deliveries", json=event)

    assert response.status_code == 200
    assert response.json()["outcome"] == "duplicate"


def test_a_held_delivery_answers_two_hundred_and_two(client):
    """Accepted and not applied.

    400 would invite them to discard it, and the message is not malformed. It
    is unreadable in a way only they can settle.
    """
    response = client.post(
        "/partners/lindhoff/deliveries",
        json=lindhoff_row(external_ref="LH-API-1", status="HOLD"),
    )
    assert response.status_code == 202
    assert response.json()["outcome"] == "quarantined"


def test_a_version_collision_answers_four_oh_nine(client):
    """The one case where the partner really does have to look."""
    stamp = "2026-08-04T09:15:00+02:00"
    client.post("/partners/coralbay/deliveries", json=coralbay_event(
        event_id="api-3", booking_ref="CB-API-3", updated_at=stamp, pax=2))
    response = client.post("/partners/coralbay/deliveries", json=coralbay_event(
        event_id="api-4", booking_ref="CB-API-3", updated_at=stamp, pax=5))

    assert response.status_code == 409
    assert response.json()["outcome"] == "conflict"


def test_an_unknown_partner_is_a_four_oh_four(client):
    response = client.post("/partners/nobody/deliveries", json={})
    assert response.status_code == 404


def test_the_reconciliation_endpoint_returns_the_report(client):
    client.post("/partners/coralbay/deliveries",
                json=coralbay_event(event_id="api-5", booking_ref="CB-API-4"))
    response = client.get(f"/reports/reconciliation/{PERIOD}")

    assert response.status_code == 200
    assert "reconciliation" in response.text
    assert "synthetic example" in response.text
