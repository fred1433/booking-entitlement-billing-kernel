"""The payment provider, behind one method.

Nothing in this repository has ever held a payment key. The kernel talks to a
provider through one method, the tests talk to a fake that reproduces the part
of the real contract that matters, and wiring the real client is the small
adapter at the bottom of this file plus one value from the environment.

The part of the contract that matters is the idempotency key, and it has two
halves that are easy to get half right:

* the same key with the same request returns the first charge, and takes no
  money a second time;
* the same key with a *different* request is an error, not a second charge and
  not a silent success. A key that is reused for a different amount is a bug in
  the caller, and the provider says so rather than guessing which amount was
  meant.

That second half is why the kernel derives its keys from (entitlement, period)
and derives the amount from the same row: if the amount can move under a fixed
key, the protection is gone.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Protocol

from ..clock import utc_now


@dataclass(frozen=True)
class Charge:
    id: str
    idempotency_key: str
    amount_cents: int
    currency: str
    created_at: datetime
    replayed: bool = False


class IdempotencyConflict(RuntimeError):
    """The same key was presented with a different request."""


class ProviderUnavailable(RuntimeError):
    """The provider did not answer. Whether it took the money is unknown."""


class PaymentProvider(Protocol):
    def create_charge(
        self,
        idempotency_key: str,
        amount_cents: int,
        currency: str,
        metadata: dict[str, str],
    ) -> Charge:
        ...


@dataclass
class FakeStripe:
    """A payment provider that keeps the promises the real one keeps.

    ``attempts`` counts every call. ``charges`` counts the ones that moved
    money. A test that asserts on both is a test that can tell "we retried"
    from "we charged twice", which is the only distinction that matters here.
    """

    _by_key: dict[str, Charge] = field(default_factory=dict)
    _requests: dict[str, tuple[int, str]] = field(default_factory=dict)
    attempts: int = 0
    charges: int = 0
    fail_before_response: Callable[[str], bool] | None = None

    def create_charge(
        self,
        idempotency_key: str,
        amount_cents: int,
        currency: str,
        metadata: dict[str, str] | None = None,
    ) -> Charge:
        self.attempts += 1

        previous = self._requests.get(idempotency_key)
        if previous is not None and previous != (amount_cents, currency):
            raise IdempotencyConflict(
                f"key {idempotency_key} was first used for {previous[0]} {previous[1]} "
                f"and is now presented for {amount_cents} {currency}"
            )

        if idempotency_key in self._by_key:
            existing = self._by_key[idempotency_key]
            return Charge(
                id=existing.id,
                idempotency_key=idempotency_key,
                amount_cents=existing.amount_cents,
                currency=existing.currency,
                created_at=existing.created_at,
                replayed=True,
            )

        # The money moves here, before the caller hears anything back. A
        # provider that dies at this instant is the case the kernel is built
        # for: the charge exists and the caller does not know it.
        self.charges += 1
        charge = Charge(
            id=f"ch_fake_{self.charges:06d}",
            idempotency_key=idempotency_key,
            amount_cents=amount_cents,
            currency=currency,
            created_at=utc_now(),
        )
        self._by_key[idempotency_key] = charge
        self._requests[idempotency_key] = (amount_cents, currency)

        if self.fail_before_response is not None and self.fail_before_response(idempotency_key):
            raise ProviderUnavailable(
                f"the charge for {idempotency_key} was created and the response was lost"
            )
        return charge

    def charge_count_for(self, idempotency_key: str) -> int:
        return 1 if idempotency_key in self._by_key else 0


@dataclass
class StripePaymentProvider:
    """The real provider, as a thin adapter over an injected client.

    ``create_payment_intent`` is whatever the payment library exposes, passed in
    by the caller. Keeping it injected is what lets the test below prove the
    derived idempotency key reaches the call, without a key, a network, or a
    dependency on the library being installed.
    """

    create_payment_intent: Callable[[dict, str], dict]

    def create_charge(
        self,
        idempotency_key: str,
        amount_cents: int,
        currency: str,
        metadata: dict[str, str] | None = None,
    ) -> Charge:
        response = self.create_payment_intent(
            {
                "amount": amount_cents,
                "currency": currency.lower(),
                "metadata": metadata or {},
                "confirm": True,
            },
            idempotency_key,
        )
        return Charge(
            id=str(response["id"]),
            idempotency_key=idempotency_key,
            amount_cents=amount_cents,
            currency=currency,
            created_at=utc_now(),
        )
