"""Test fixtures.

The tests run against a real PostgreSQL, on the schema the migration produced,
because half of what this kernel promises is kept by unique indexes and a
sqlite substitute would quietly stop testing exactly the half that matters.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from kernel.config import Settings
from kernel.db import make_engine, make_session_factory
from kernel.models import Base
from kernel.partners import CoralbayAdapter, LindhoffAdapter

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TEST_URL = "postgresql+psycopg://postgres:postgres@localhost:5432/kernel_test"


def _database_url() -> str:
    return os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL") or DEFAULT_TEST_URL


@pytest.fixture(scope="session")
def engine():
    url = _database_url()
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT,
        env={**os.environ, "DATABASE_URL": url},
        check=True,
        capture_output=True,
    )
    engine = make_engine(url)
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def session_factory(engine):
    return make_session_factory(engine)


def truncate_all(engine) -> None:
    tables = ", ".join(f'"{name}"' for name in Base.metadata.tables)
    with engine.begin() as connection:
        connection.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))


@pytest.fixture(autouse=True)
def clean(engine):
    truncate_all(engine)
    yield


@pytest.fixture
def session(session_factory) -> Session:
    session = session_factory()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture
def other_session(session_factory) -> Session:
    """A second connection, for the races that only a second connection shows."""
    session = session_factory()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture
def settings() -> Settings:
    """Test settings. The secret is a test value and only ever a test value."""
    return Settings(
        database_url=_database_url(),
        claim_token_secret="test-secret-not-used-anywhere-else",
        claim_token_ttl_seconds=3600,
        share_limit=3,
    )


def wait_until_blocked(engine, timeout: float = 10.0) -> None:
    """Block until another connection is waiting on a row lock.

    Polling the server is what makes the race test deterministic. A sleep would
    make it flaky on a loaded machine, which is the same thing as not testing
    the race at all.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with engine.connect() as connection:
            waiting = connection.execute(
                text(
                    "select count(*) from pg_stat_activity "
                    "where wait_event_type = 'Lock' and datname = current_database()"
                )
            ).scalar_one()
        if waiting:
            return
        time.sleep(0.02)
    raise AssertionError("no connection ever blocked on a lock")


@pytest.fixture
def coralbay() -> CoralbayAdapter:
    return CoralbayAdapter()


@pytest.fixture
def lindhoff() -> LindhoffAdapter:
    return LindhoffAdapter()


# --------------------------------------------------------------------------- #
# Payload builders. Both partners are fictional.
# --------------------------------------------------------------------------- #

def coralbay_event(
    event_id: str = "evt-1",
    booking_ref: str = "CB-1001",
    state: str = "confirmed",
    updated_at: str = "2026-08-04T09:15:00+02:00",
    amount_minor: int = 24000,
    pax: int = 2,
    **overrides: object,
) -> dict:
    payload = {
        "event_id": event_id,
        "booking_ref": booking_ref,
        "state": state,
        "guest_reference": "guest-77",
        "rate_plan": "SEA-VIEW-FLEX",
        "pax": pax,
        "check_in": "2026-08-10T14:00:00+02:00",
        "check_out": "2026-08-14T10:00:00+02:00",
        "amount_minor": amount_minor,
        "currency": "EUR",
        "updated_at": updated_at,
    }
    payload.update(overrides)
    return payload


def lindhoff_row(
    external_ref: str = "LH-5001",
    status: str = "OK",
    changed_at: str = "2026-08-04 09:15:00",
    price_cents: int = 18000,
    units: int = 1,
    **overrides: object,
) -> dict:
    row = {
        "external_ref": external_ref,
        "status": status,
        "cust": "cust-31",
        "article": "CITY-PASS-72H",
        "units": units,
        "from": "2026-08-10",
        "to": "2026-08-13",
        "price_cents": price_cents,
        "curr": "EUR",
        "changed_at": changed_at,
    }
    row.update(overrides)
    return row


def lindhoff_csv(rows: list[dict], header: list[str] | None = None,
                 trailer: int | None = None, terminated: bool = True) -> str:
    header = header or ["external_ref", "status", "cust", "article", "units",
                        "from", "to", "price_cents", "curr", "changed_at"]
    lines = [",".join(header)]
    for row in rows:
        lines.append(",".join(str(row[column]) for column in header))
    body = "\n".join(lines) + ("\n" if terminated else "")
    if trailer is not None:
        body += f"#rows={trailer}\n"
    return body


def utc(text_value: str) -> datetime:
    return datetime.fromisoformat(text_value).astimezone(timezone.utc)


DAY = timedelta(days=1)
