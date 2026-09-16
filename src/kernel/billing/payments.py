"""The external system, behind one method, and a simulation of it.

Nothing in this repository has ever held a payment key and nothing in it makes
a network call. The sample talks to an external system through one method; the
tests talk to a simulation of it.

The simulation is deliberately not a mock. It is a small service backed by its
own table on its own connection, which means two things a mock cannot give:

* the effect it records is **durable**. It survives the caller's rollback and
  the caller's restart, so "the external operation succeeded and the answer was
  lost" can actually be staged instead of asserted;
* it can be asked, afterwards, how many operations really happened. A test that
  can tell "we retried" from "we did it twice" is the only kind worth writing
  here.

It reproduces three behaviours that the Stripe documentation describes for
idempotent creates. They are the reason the caller is built the way it is:

1. the same key with the **same** parameters replays the stored result;
2. the same key with **different** parameters is an error, not a second
   operation and not a silent success;
3. a key is only remembered for a retention window (24 hours in the
   documentation). Presenting it after that does not replay anything: it
   creates a new request. So a retry is only safe inside the window, and
   outside it the caller must establish the outcome another way rather than
   send the same intent again.

A failure to answer is not a failure to act. An indeterminate response means
the outcome is unknown, and the one thing that must never follow is the same
intent under a fresh key.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Protocol

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from ..clock import utc_now
from ..models import SimulatedProviderCharge

#: The retention window the Stripe documentation gives for idempotency keys.
KEY_RETENTION = timedelta(hours=24)


def request_fingerprint(amount_cents: int, currency: str, metadata: dict | None = None) -> str:
    """The parameters of the request, frozen.

    Recorded next to the intent before the call, and compared on every retry. A
    key that stays put while the parameters move is a key that protects
    nothing.
    """
    body = json.dumps(
        {"amount": amount_cents, "currency": currency.lower(), "metadata": metadata or {}},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(body.encode()).hexdigest()[:32]


@dataclass(frozen=True)
class ChargeResult:
    charge_ref: str
    idempotency_key: str
    amount_cents: int
    currency: str
    created_at: datetime
    replayed: bool = False


class IdempotencyConflict(RuntimeError):
    """The same key was presented with different parameters."""


class ProviderUnavailable(RuntimeError):
    """No answer came back. Whether the operation happened is unknown."""


class KeyNoLongerRetained(RuntimeError):
    """The key is outside the retention window.

    Presenting it again would create a new request rather than replay the old
    one, so the caller must not present it again.
    """


class PaymentProvider(Protocol):
    def create_charge(
        self,
        idempotency_key: str,
        fingerprint: str,
        amount_cents: int,
        currency: str,
        metadata: dict[str, str] | None = None,
    ) -> ChargeResult:
        ...


class SimulatedProvider:
    """A stand-in external system whose state outlives the caller.

    ``lose_response_for`` is called **after** the effect has been committed. It
    is how a lost answer is staged: the operation happened, and the caller is
    told nothing.
    """

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        retention: timedelta = KEY_RETENTION,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self._sessions = session_factory
        self._retention = retention
        self._now = now
        self.attempts = 0
        self.lose_response_for: Callable[[str], bool] | None = None

    # -- the one method the caller sees ----------------------------------- #

    def create_charge(
        self,
        idempotency_key: str,
        fingerprint: str,
        amount_cents: int,
        currency: str,
        metadata: dict[str, str] | None = None,
    ) -> ChargeResult:
        self.attempts += 1

        with self._sessions() as session:
            existing = session.scalar(
                select(SimulatedProviderCharge).where(
                    SimulatedProviderCharge.idempotency_key == idempotency_key
                )
            )

            if existing is not None:
                age = self._now() - existing.created_at
                if age > self._retention:
                    # The key has been forgotten. A real provider would treat
                    # this as a new request and act a second time, so the
                    # simulation refuses to pretend otherwise.
                    raise KeyNoLongerRetained(
                        f"key {idempotency_key} is {int(age.total_seconds() // 3600)}h old, "
                        f"past the {int(self._retention.total_seconds() // 3600)}h retention window"
                    )
                if existing.request_fingerprint != fingerprint:
                    raise IdempotencyConflict(
                        f"key {idempotency_key} was first used with different parameters"
                    )
                return ChargeResult(
                    charge_ref=existing.charge_ref,
                    idempotency_key=idempotency_key,
                    amount_cents=existing.amount_cents,
                    currency=existing.currency,
                    created_at=existing.created_at,
                    replayed=True,
                )

            # The effect happens here, and is committed here, before the caller
            # hears anything back.
            count = session.scalar(select(func.count()).select_from(SimulatedProviderCharge)) or 0
            record = SimulatedProviderCharge(
                idempotency_key=idempotency_key,
                request_fingerprint=fingerprint,
                charge_ref=f"sim_{count + 1:06d}",
                amount_cents=amount_cents,
                currency=currency,
                created_at=self._now(),
            )
            session.add(record)
            session.commit()
            result = ChargeResult(
                charge_ref=record.charge_ref,
                idempotency_key=idempotency_key,
                amount_cents=amount_cents,
                currency=currency,
                created_at=record.created_at,
            )

        if self.lose_response_for is not None and self.lose_response_for(idempotency_key):
            raise ProviderUnavailable(
                f"the operation for {idempotency_key} was carried out and the response was lost"
            )
        return result

    # -- what a test, or a reconciliation, may ask afterwards -------------- #

    def operations_for(self, idempotency_key: str) -> int:
        with self._sessions() as session:
            return session.scalar(
                select(func.count())
                .select_from(SimulatedProviderCharge)
                .where(SimulatedProviderCharge.idempotency_key == idempotency_key)
            ) or 0

    def total_operations(self) -> int:
        with self._sessions() as session:
            return session.scalar(select(func.count()).select_from(SimulatedProviderCharge)) or 0

    def lookup(self, idempotency_key: str) -> ChargeResult | None:
        """Establish an outcome out of band.

        This is the operation an engineer performs by hand, or against the
        provider's search API, when a key has fallen outside the retention
        window and the intent is still unresolved.
        """
        with self._sessions() as session:
            found = session.scalar(
                select(SimulatedProviderCharge).where(
                    SimulatedProviderCharge.idempotency_key == idempotency_key
                )
            )
            if found is None:
                return None
            return ChargeResult(
                charge_ref=found.charge_ref,
                idempotency_key=idempotency_key,
                amount_cents=found.amount_cents,
                currency=found.currency,
                created_at=found.created_at,
                replayed=True,
            )


@dataclass
class InjectedClientProvider:
    """The shape a real client would take, as a thin adapter.

    ``create`` is whatever the payment library exposes, passed in by the
    caller. Keeping it injected is what lets a test prove that the derived key
    and the frozen parameters reach the call, without a key, a network, or the
    library being installed.
    """

    create: Callable[[dict, str], dict]

    def create_charge(
        self,
        idempotency_key: str,
        fingerprint: str,
        amount_cents: int,
        currency: str,
        metadata: dict[str, str] | None = None,
    ) -> ChargeResult:
        response = self.create(
            {
                "amount": amount_cents,
                "currency": currency.lower(),
                "metadata": metadata or {},
            },
            idempotency_key,
        )
        return ChargeResult(
            charge_ref=str(response["id"]),
            idempotency_key=idempotency_key,
            amount_cents=amount_cents,
            currency=currency,
            created_at=utc_now(),
        )
