"""The HTTP surface.

Three status codes, and the reason for each one is an operational reason rather
than a taxonomic one: what does this answer make a well behaved partner do
next.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from conftest import coralbay_event, lindhoff_row
from kernel.api import create_app


@pytest.fixture
def client(session_factory, settings):
    return TestClient(create_app(session_factory=session_factory, settings=settings))


def test_a_new_delivery_is_applied(client):
    response = client.post("/partners/coralbay/deliveries", json=coralbay_event(booking_ref="CB-3001"))
    assert response.status_code == 200
    assert response.json()["outcome"] == "applied"


def test_a_redelivered_event_answers_two_hundred(client):
    """Anything else puts a non problem back into the partner's retry queue."""
    event = coralbay_event(event_id="evt-3002", booking_ref="CB-3002")
    client.post("/partners/coralbay/deliveries", json=event)
    response = client.post("/partners/coralbay/deliveries", json=event)

    assert response.status_code == 200
    assert response.json()["outcome"] == "duplicate"


def test_an_unusable_delivery_answers_two_hundred_and_two(client):
    """We have it, we are not acting on it, and resending the same bytes will not help."""
    response = client.post("/partners/lindhoff/deliveries",
                           json=lindhoff_row(external_ref="LH-3003", status="HOLD"))
    assert response.status_code == 202
    assert response.json()["outcome"] == "quarantined"


def test_an_unknown_partner_is_a_four_oh_four(client):
    assert client.post("/partners/nobody/deliveries", json={}).status_code == 404


def test_a_spent_claim_link_answers_gone(client, session, session_factory, settings):
    from sqlalchemy import select

    from kernel.entitlements import issue_claim_token
    from kernel.models import Entitlement

    client.post("/partners/coralbay/deliveries", json=coralbay_event(booking_ref="CB-3004"))
    entitlement = session.execute(select(Entitlement)).scalar_one()
    token = issue_claim_token(session, entitlement, settings)
    session.commit()

    first = client.post(f"/claims/{token}", json={"claimed_by": "holder"})
    second = client.post(f"/claims/{token}", json={"claimed_by": "holder"})

    assert first.status_code == 200 and first.json()["state"] == "claimed"
    assert second.status_code == 410


def test_the_reconciliation_endpoint_returns_the_report(client):
    client.post("/partners/lindhoff/deliveries", json=lindhoff_row(external_ref="LH-3005", status="HOLD"))
    response = client.get("/reports/reconciliation/2026-08")

    assert response.status_code == 200
    body = response.json()
    assert body["period"] == "2026-08"
    assert body["waiting_on_partners"][0]["kind"] == "unknown_status_value"
