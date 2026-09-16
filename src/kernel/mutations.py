"""Protections that can be switched off on purpose.

A test suite that passes is not evidence. A test suite that fails when the
protection it claims to test is removed is evidence, and the difference between
the two is the difference between a green badge and a reason to believe one.

Every switch here disables exactly one control. ``tests/test_protections_are_load_bearing.py``
turns each of them off and asserts that the dangerous outcome then happens: a
second external operation, a silently overwritten booking, a charge prepared
from data nobody could decide. Nothing in normal operation reads these as
anything other than off.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

#: The idempotency key is derived from (entitlement, period). Disabling this
#: generates a fresh key per attempt, which is how a retry becomes a second
#: operation.
DERIVED_IDEMPOTENCY_KEY = "derived_idempotency_key"

#: The intent is written and committed before the call. Disabling this sends
#: first and records afterwards, which is how a crash loses the evidence that
#: anything was ever attempted.
INTENT_BEFORE_CALL = "intent_before_call"

#: An undecidable booking is not prepared for billing. Disabling this bills on
#: data nobody could interpret.
UNDECIDED_BLOCKS_BILLING = "undecided_blocks_billing"

#: Same identity, same version, different content is a conflict. Disabling this
#: takes the last arrival, which is a coin toss nobody is told about.
CONFLICT_IS_NOT_A_DUPLICATE = "conflict_is_not_a_duplicate"

_disabled: set[str] = set()


def is_disabled(name: str) -> bool:
    return name in _disabled


@contextmanager
def without(name: str) -> Iterator[None]:
    """Run a block with one protection removed."""
    _disabled.add(name)
    try:
        yield
    finally:
        _disabled.discard(name)
