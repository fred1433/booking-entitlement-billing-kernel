"""The HTTP surface, deliberately thin.

One decision worth writing down, because it is the one that gets an integration
stuck in a retry storm on day three:

* a delivery the kernel has already seen answers 200 with ``duplicate``. It is
  the truth, and any other answer puts a non problem back into the partner's
  retry queue;
* a delivery the kernel could not use answers 202 with ``quarantined``. We have
  it, we are not acting on it, and redelivering the same bytes will not change
  that. It is our question to ask, not their message to resend;
* only a genuine failure on our side answers 5xx, because 5xx is the one answer
  that means "send it again".
"""

from __future__ import annotations

from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Response, status
from pydantic import BaseModel
from sqlalchemy.orm import Session, sessionmaker

from ..config import Settings
from ..db import make_engine, make_session_factory
from ..entitlements.claim import ClaimRefused, consume_claim_token
from ..intake.service import apply_delivery
from ..models import DeliveryOutcome
from ..billing.reconciliation import reconcile, render_reconciliation
from ..partners import CORALBAY, LINDHOFF, CoralbayAdapter, LindhoffAdapter

ADAPTERS = {
    CORALBAY: CoralbayAdapter(),
    LINDHOFF: LindhoffAdapter(),
}

ACCEPTED_OUTCOMES = {
    DeliveryOutcome.APPLIED,
    DeliveryOutcome.DUPLICATE,
    DeliveryOutcome.SUPERSEDED,
}


class ClaimRequest(BaseModel):
    claimed_by: str


def create_app(session_factory: sessionmaker[Session] | None = None,
               settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    factory = session_factory or make_session_factory(make_engine(settings.database_url))
    app = FastAPI(title="booking to billing integration kernel", version="0.1.0")

    def get_session() -> Any:
        session = factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/partners/{partner}/deliveries")
    def receive(partner: str, payload: dict, response: Response,
                session: Session = Depends(get_session)) -> dict[str, Any]:
        adapter = ADAPTERS.get(partner)
        if adapter is None:
            raise HTTPException(status_code=404, detail=f"unknown partner {partner}")

        result = apply_delivery(session, adapter, payload, channel="push")
        if result.outcome not in ACCEPTED_OUTCOMES:
            response.status_code = status.HTTP_202_ACCEPTED
        return {
            "outcome": result.outcome.value,
            "delivery_id": result.delivery_id,
            "booking_id": result.booking_id,
            "reason": result.reason,
        }

    @app.post("/claims/{token}")
    def claim(token: str, body: ClaimRequest,
              session: Session = Depends(get_session)) -> dict[str, Any]:
        try:
            entitlement = consume_claim_token(session, token, body.claimed_by, settings)
        except ClaimRefused as error:
            raise HTTPException(status_code=410, detail=error.reason) from error
        return {"entitlement_id": entitlement.id, "state": entitlement.state}

    @app.get("/reports/reconciliation/{period}")
    def reconciliation(period: str, session: Session = Depends(get_session)) -> dict[str, Any]:
        report = reconcile(session, period)
        return {
            "period": report.period,
            "generated_at": report.generated_at,
            "lines": [
                {"state": row.state, "count": row.count, "amount_cents": row.amount_cents}
                for row in report.by_state
            ],
            "waiting_on_partners": [
                {
                    "partner": item.partner,
                    "external_id": item.external_id,
                    "kind": item.kind,
                    "needed_from_partner": item.needed_from_partner,
                    "days_open": item.days_open,
                }
                for item in report.waiting_on_partners
            ],
            "text": render_reconciliation(report),
        }

    return app
