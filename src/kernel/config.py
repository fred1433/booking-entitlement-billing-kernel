"""Configuration, read from the environment. No secret is committed here."""

from __future__ import annotations

import os
from dataclasses import dataclass


DEFAULT_DATABASE_URL = "postgresql+psycopg://postgres:postgres@localhost:5432/kernel_dev"


@dataclass(frozen=True)
class Settings:
    database_url: str
    claim_token_secret: str
    claim_token_ttl_seconds: int
    share_limit: int

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            database_url=os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL),
            # Development default only. Production reads a real value from the
            # environment; the kernel refuses to issue claim links if this is
            # left at the default outside of development (see claim.py).
            claim_token_secret=os.environ.get("CLAIM_TOKEN_SECRET", "dev-only-not-a-secret"),
            claim_token_ttl_seconds=int(os.environ.get("CLAIM_TOKEN_TTL_SECONDS", "604800")),
            share_limit=int(os.environ.get("ENTITLEMENT_SHARE_LIMIT", "3")),
        )


DEV_SECRET_PLACEHOLDER = "dev-only-not-a-secret"

# The maximum number of shares a single entitlement can carry. This constant is
# mirrored by a CHECK constraint in the schema, so the limit survives a bug in
# the service layer and a race between two concurrent requests.
MAX_SHARES_PER_ENTITLEMENT = 3
