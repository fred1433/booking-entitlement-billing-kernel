"""The entitlement lifecycle, written once, in one place.

Every transition the product is allowed to make is in this table. Anything
absent from it is refused and journalled, including the ones that look
harmless: re-opening a cancelled entitlement, claiming one twice, activating
one that was never claimed.

Keeping the table separate from the code that uses it is the point. A state
machine scattered across service methods is a state machine nobody can read,
and a lifecycle nobody can read is a lifecycle nobody can bill correctly.
"""

from __future__ import annotations

from ..models import EntitlementState as S

ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    S.CREATED.value: frozenset({S.CLAIMABLE.value, S.CANCELLED.value}),
    S.CLAIMABLE.value: frozenset({S.CLAIMED.value, S.CANCELLED.value, S.EXPIRED.value}),
    S.CLAIMED.value: frozenset({S.ACTIVE.value, S.CANCELLED.value, S.EXPIRED.value}),
    S.ACTIVE.value: frozenset({S.CANCELLED.value, S.EXPIRED.value}),
    S.CANCELLED.value: frozenset(),
    S.EXPIRED.value: frozenset(),
}

TERMINAL: frozenset[str] = frozenset({S.CANCELLED.value, S.EXPIRED.value})

#: States in which an entitlement is worth money for a period it overlaps.
BILLABLE: frozenset[str] = frozenset({S.CLAIMED.value, S.ACTIVE.value})


def is_allowed(current: str, target: str) -> bool:
    return target in ALLOWED_TRANSITIONS.get(current, frozenset())
