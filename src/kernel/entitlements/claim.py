"""The passwordless claim link.

Two separate properties, two separate mechanisms, because they fail
differently:

* A signature makes a forged or mistyped link cheap to reject. No database
  round trip, so a scanner walking the URL space costs us nothing.
* A unique hash and a conditional update make a valid link usable exactly once.
  The check and the consumption are one statement, so two people opening the
  same link at the same moment cannot both be let in.

The kernel stores the hash, never the token. A copy of this table is not a set
of working claim links.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import timedelta

from sqlalchemy import update
from sqlalchemy.orm import Session

from .. import journal
from ..clock import utc_now
from ..config import DEV_SECRET_PLACEHOLDER, Settings
from ..models import ClaimToken, Entitlement, EntitlementState
from .service import transition


class ClaimRefused(RuntimeError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _signature(secret: str, entitlement_id: int, nonce: str) -> str:
    return hmac.new(
        secret.encode("utf-8"),
        f"{entitlement_id}.{nonce}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()[:32]


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def issue_claim_token(
    session: Session,
    entitlement: Entitlement,
    settings: Settings | None = None,
    ttl_seconds: int | None = None,
    allow_dev_secret: bool = False,
) -> str:
    """Return the claim token. This is the only moment it exists in clear."""
    settings = settings or Settings.from_env()
    if settings.claim_token_secret == DEV_SECRET_PLACEHOLDER and not allow_dev_secret:
        raise RuntimeError(
            "CLAIM_TOKEN_SECRET is still the development placeholder. Set a real "
            "value before issuing claim links."
        )
    if entitlement.state != EntitlementState.CLAIMABLE.value:
        journal.refused(session, "claim.issue", f"entitlement/{entitlement.id}",
                        f"state is {entitlement.state}, not claimable")
        session.flush()
        raise ClaimRefused(f"entitlement is {entitlement.state}, not claimable")

    nonce = secrets.token_urlsafe(24)
    token = f"{entitlement.id}.{nonce}.{_signature(settings.claim_token_secret, entitlement.id, nonce)}"
    ttl = ttl_seconds if ttl_seconds is not None else settings.claim_token_ttl_seconds

    session.add(
        ClaimToken(
            entitlement_id=entitlement.id,
            token_hash=_hash(token),
            expires_at=utc_now() + timedelta(seconds=ttl),
        )
    )
    journal.accepted(session, "claim.issue", f"entitlement/{entitlement.id}",
                     None, {"expires_in_seconds": ttl})
    session.flush()
    return token


def consume_claim_token(
    session: Session,
    token: str,
    claimed_by: str,
    settings: Settings | None = None,
) -> Entitlement:
    """Spend a claim token. At most once, ever."""
    settings = settings or Settings.from_env()
    parts = token.split(".")
    if len(parts) != 3 or not parts[0].isdigit():
        raise ClaimRefused("malformed token")

    entitlement_id, nonce, signature = int(parts[0]), parts[1], parts[2]
    expected = _signature(settings.claim_token_secret, entitlement_id, nonce)
    if not hmac.compare_digest(signature, expected):
        journal.refused(session, "claim.consume", f"entitlement/{entitlement_id}",
                        "signature does not match")
        session.flush()
        raise ClaimRefused("signature does not match")

    now = utc_now()
    # One statement: find it, prove it is unspent and unexpired, spend it.
    # Splitting this into a read and a write is what lets two people open the
    # same link at the same moment and both get in.
    consumed = session.execute(
        update(ClaimToken)
        .where(
            ClaimToken.token_hash == _hash(token),
            ClaimToken.consumed_at.is_(None),
            ClaimToken.expires_at > now,
        )
        .values(consumed_at=now, consumed_by=claimed_by)
        .returning(ClaimToken.id, ClaimToken.entitlement_id)
    ).one_or_none()

    if consumed is None:
        journal.refused(session, "claim.consume", f"entitlement/{entitlement_id}",
                        "token is unknown, already spent, or expired")
        session.flush()
        raise ClaimRefused("token is unknown, already spent, or expired")

    entitlement = session.get(Entitlement, consumed.entitlement_id)
    transition(session, entitlement, EntitlementState.CLAIMED.value, f"claimed by {claimed_by}")
    entitlement.holder_ref = claimed_by
    session.flush()
    return entitlement
