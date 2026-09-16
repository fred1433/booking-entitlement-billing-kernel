"""A deliberately small HTTP surface.

Two routes. This sample is not a service to deploy, and a larger API would be
more code without another failure path. What is here exists because the intake
rules have to be reachable the way a partner would reach them, and because the
status code a partner receives is itself a decision:

* **200** for a delivery that was applied, and for one that was recognised as a
  repeat. A partner retrying must be told "I have this", not "try again", or a
  correct retry policy on their side turns into a storm on ours.
* **202** for a delivery that was accepted and held. It was recorded, it was
  not applied, and a question is open. Answering 400 would invite them to
  discard it, and the message is not malformed, it is unreadable in a way only
  they can settle.
* **409** for the one case where the partner really must look: two versions of
  one booking carrying the same identity and the same version.

Nothing here is deployed anywhere.
"""

from __future__ import annotations

from fastapi import Body, FastAPI, HTTPException, Response
from sqlalchemy.orm import sessionmaker

from ..billing.reconciliation import reconcile, render_reconciliation
from ..db import make_engine, make_session_factory
from ..intake import apply_delivery
from ..models import DeliveryOutcome
from ..partners import ADAPTERS

_HELD = {
    DeliveryOutcome.QUARANTINED,
    DeliveryOutcome.ORDER_NOT_PROVABLE,
    DeliveryOutcome.HELD,
}


def create_app(session_factory: sessionmaker | None = None) -> FastAPI:
    sessions = session_factory or make_session_factory(make_engine())
    app = FastAPI(title="booking to billing failure cases", docs_url=None, redoc_url=None)

    @app.post("/partners/{partner}/deliveries")
    def receive(partner: str, response: Response, payload: dict = Body(...)) -> dict:
        adapter = ADAPTERS.get(partner)
        if adapter is None:
            raise HTTPException(status_code=404, detail=f"unknown partner {partner!r}")

        with sessions() as session:
            result = apply_delivery(session, adapter, payload, channel="push")
            session.commit()
            outcome, delivery_id, reason = result.outcome, result.delivery_id, result.reason

        if outcome is DeliveryOutcome.CONFLICT:
            response.status_code = 409
        elif outcome in _HELD:
            response.status_code = 202
        return {"outcome": outcome.value, "delivery_id": delivery_id, "detail": reason}

    @app.get("/reports/reconciliation/{period}")
    def reconciliation(period: str) -> Response:
        with sessions() as session:
            report = reconcile(session, period)
            body = render_reconciliation(session, report)
        return Response(content=body, media_type="text/plain")

    return app
