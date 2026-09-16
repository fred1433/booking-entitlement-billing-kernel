"""The schema.

Several of the promises in this sample are kept by the database rather than by
the service layer, because the service layer is the part a future change can
walk around by accident:

1. one booking per (partner, external_id)          unique index
2. one application per partner event id            unique index
3. one billing line per (entitlement, period)      unique index
4. one durable intent per idempotency key          unique index
5. one simulated external charge per key           unique index

``tests/test_database_guarantees.py`` proves each one by trying to break it in
raw SQL, with the service layer out of the way.

Both identity keys below are **assumptions, not facts**: that
``(partner, external_id)`` names one booking for its whole life, and that
``(entitlement, period)`` is the billable unit. They are written down in
``docs/what-needs-confirmation.md`` as questions, because neither can be
established from our side.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


# --------------------------------------------------------------------------- #
# Vocabularies
# --------------------------------------------------------------------------- #

class DeliveryOutcome(str, Enum):
    APPLIED = "applied"
    DUPLICATE = "duplicate"          # same event id, delivered again
    SUPERSEDED = "superseded"        # provably older than what we hold
    CONFLICT = "conflict"            # same identity, same version, different content
    ORDER_NOT_PROVABLE = "order_not_provable"
    QUARANTINED = "quarantined"      # unusable as received
    HELD = "held"                    # part of a batch that was not applied


class BookingStatus(str, Enum):
    CONFIRMED = "confirmed"
    AMENDED = "amended"
    CANCELLED = "cancelled"


class EntitlementState(str, Enum):
    """Deliberately two states.

    A real product has a lifecycle. This sample is not the product: it exists
    to exercise failure paths, and a larger lifecycle would add states without
    adding a failure path.
    """

    ACTIVE = "active"
    CANCELLED = "cancelled"


class LineState(str, Enum):
    PREPARED = "prepared"            # intent recorded, nothing submitted
    SUBMITTED = "submitted"          # sent to the external system, outcome unknown
    CHARGED = "charged"              # external result established
    VOIDED = "voided"                # cancelled before anything was submitted
    BLOCKED = "blocked"              # source data is undecidable, do not prepare
    UNRESOLVED = "unresolved"        # outcome unknown and no longer safely retryable


class Cancellation(str, Enum):
    NONE = "none"
    BEFORE_SUBMISSION = "before_submission"
    WHILE_UNRESOLVED = "while_unresolved"
    AFTER_CHARGE = "after_charge"


class BatchState(str, Enum):
    APPLIED = "applied"
    REJECTED_INCOMPLETE = "rejected_incomplete"
    HELD_FOR_REVIEW = "held_for_review"


# --------------------------------------------------------------------------- #
# Intake
# --------------------------------------------------------------------------- #

class Delivery(Base):
    """Every message received, with what was done about it.

    Append only. A duplicate is written here too: the journal answers "did you
    get my message" before it answers "did you act on it", and those are
    different questions when a partner is asking.
    """

    __tablename__ = "deliveries"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    partner: Mapped[str] = mapped_column(String(64), index=True)
    channel: Mapped[str] = mapped_column(String(16))          # push, file
    source_event_id: Mapped[str | None] = mapped_column(String(128))
    external_id: Mapped[str | None] = mapped_column(String(128), index=True)
    payload: Mapped[dict] = mapped_column(JSONB)
    payload_fingerprint: Mapped[str | None] = mapped_column(String(64))
    updated_at_raw: Mapped[str | None] = mapped_column(String(64))
    updated_at_lo: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at_hi: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at_ambiguous: Mapped[bool] = mapped_column(Boolean, default=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    batch_id: Mapped[str | None] = mapped_column(String(64), index=True)
    outcome: Mapped[str] = mapped_column(String(32), index=True)
    outcome_reason: Mapped[str | None] = mapped_column(Text)
    booking_id: Mapped[int | None] = mapped_column(ForeignKey("bookings.id"))


class ProcessedEvent(Base):
    """The at-least-once control.

    A partner that retries sends the same event id again. This table is what
    makes the second arrival a no-op, and it is a unique index rather than a
    lookup in code so that two retries arriving at the same moment on two
    workers cannot both win.
    """

    __tablename__ = "processed_events"
    __table_args__ = (
        UniqueConstraint("partner", "source_event_id", name="uq_processed_event"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    partner: Mapped[str] = mapped_column(String(64))
    source_event_id: Mapped[str] = mapped_column(String(128))
    first_delivery_id: Mapped[int] = mapped_column(BigInteger)
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Booking(Base):
    """One booking, in this sample's own vocabulary."""

    __tablename__ = "bookings"
    __table_args__ = (
        UniqueConstraint("partner", "external_id", name="uq_booking_identity"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    partner: Mapped[str] = mapped_column(String(64))
    external_id: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(32))
    customer_ref: Mapped[str] = mapped_column(String(128))
    product_code: Mapped[str] = mapped_column(String(64))
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    amount_cents: Mapped[int] = mapped_column(Integer)
    amount_basis: Mapped[str] = mapped_column(String(16), default="per_unit")
    currency: Mapped[str] = mapped_column(String(3))

    updated_at_raw: Mapped[str] = mapped_column(String(64))
    updated_at_lo: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at_hi: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at_ambiguous: Mapped[bool] = mapped_column(Boolean, default=False)
    payload_fingerprint: Mapped[str] = mapped_column(String(64))

    #: Set when a delivery could not be decided. While it is set, nothing about
    #: this booking is prepared for billing. Cleared when the question is
    #: answered and the delivery replayed.
    undecided_reason: Mapped[str | None] = mapped_column(Text)

    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_applied_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_delivery_id: Mapped[int | None] = mapped_column(BigInteger)

    entitlement: Mapped["Entitlement"] = relationship(back_populates="booking", uselist=False)


class VersionConflict(Base):
    """Same identity, same version, different content.

    Kept rather than resolved. Nothing here picks a winner, because nothing
    here can: two payloads carrying the same ``updated_at`` and different
    statuses do not say which one the partner sent last.
    """

    __tablename__ = "version_conflicts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    booking_id: Mapped[int] = mapped_column(ForeignKey("bookings.id"), index=True)
    delivery_id: Mapped[int] = mapped_column(BigInteger)
    held_fingerprint: Mapped[str] = mapped_column(String(64))
    incoming_fingerprint: Mapped[str] = mapped_column(String(64))
    held_payload: Mapped[dict] = mapped_column(JSONB)
    incoming_payload: Mapped[dict] = mapped_column(JSONB)
    question_for_partner: Mapped[str] = mapped_column(Text)
    raised_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class QuarantineItem(Base):
    """What is being waited on, from whom, and since when.

    Nothing is silently dropped: it is quarantined, and the reason is written
    in the partner's own terms. ``needed_from_partner`` is the sentence an
    engineer would actually send.
    """

    __tablename__ = "quarantine_items"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    partner: Mapped[str] = mapped_column(String(64), index=True)
    external_id: Mapped[str | None] = mapped_column(String(128), index=True)
    delivery_id: Mapped[int | None] = mapped_column(BigInteger)
    kind: Mapped[str] = mapped_column(String(48))
    detail: Mapped[str] = mapped_column(Text)
    needed_from_partner: Mapped[str] = mapped_column(Text)
    raised_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ImportBatch(Base):
    """One partner file, applied as a whole or not at all."""

    __tablename__ = "import_batches"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    partner: Mapped[str] = mapped_column(String(64), index=True)
    batch_id: Mapped[str] = mapped_column(String(64), unique=True)
    filename: Mapped[str] = mapped_column(String(256))
    row_count: Mapped[int] = mapped_column(Integer)
    applied_rows: Mapped[int] = mapped_column(Integer, default=0)
    quarantined_rows: Mapped[int] = mapped_column(Integer, default=0)
    state: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str | None] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# --------------------------------------------------------------------------- #
# The billable obligation
# --------------------------------------------------------------------------- #

class Entitlement(Base):
    __tablename__ = "entitlements"
    __table_args__ = (
        CheckConstraint("state in ('active','cancelled')", name="ck_entitlement_state"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    booking_id: Mapped[int] = mapped_column(ForeignKey("bookings.id"), unique=True)
    state: Mapped[str] = mapped_column(String(32), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_reason: Mapped[str | None] = mapped_column(Text)
    billable_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    billable_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    booking: Mapped[Booking] = relationship(back_populates="entitlement")


# --------------------------------------------------------------------------- #
# Billing
# --------------------------------------------------------------------------- #

class BillingRun(Base):
    __tablename__ = "billing_runs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    period: Mapped[str] = mapped_column(String(7), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    state: Mapped[str] = mapped_column(String(16), default="running")
    attempt: Mapped[int] = mapped_column(Integer, default=1)


class BillingLine(Base):
    """One line per entitlement per period, and the durable intent for it.

    The line *is* the intent. It is written, with its key and the exact
    parameters of the request, and committed, **before** anything is sent
    anywhere. That ordering is the whole point: a process that dies between the
    intent and the answer leaves a row that says what was attempted, which is
    the only thing that makes a safe resume possible.

    ``idempotency_key`` is derived from (entitlement, period), never generated
    per attempt. ``request_fingerprint`` freezes the parameters, because a key
    that stays put while the amount moves protects nothing.
    """

    __tablename__ = "billing_lines"
    __table_args__ = (
        UniqueConstraint("entitlement_id", "period", name="uq_line_per_entitlement_period"),
        UniqueConstraint("idempotency_key", name="uq_line_idempotency_key"),
        Index("ix_line_state", "state"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("billing_runs.id"))
    entitlement_id: Mapped[int] = mapped_column(ForeignKey("entitlements.id"))
    period: Mapped[str] = mapped_column(String(7))
    amount_cents: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3))

    idempotency_key: Mapped[str] = mapped_column(String(128))
    request_fingerprint: Mapped[str | None] = mapped_column(String(64))

    state: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str | None] = mapped_column(Text)
    cancellation: Mapped[str] = mapped_column(String(24), default=Cancellation.NONE.value)
    charge_ref: Mapped[str | None] = mapped_column(String(128))

    prepared_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# --------------------------------------------------------------------------- #
# The simulated external system
# --------------------------------------------------------------------------- #

class SimulatedProviderCharge(Base):
    """The stand-in payment provider's own store.

    It lives in PostgreSQL, in its own table, for one reason: a fake that keeps
    its state in the worker's memory cannot show what happens when the worker
    restarts, which is the only interesting moment in this whole repository.

    This is a simulation. It is not Stripe, it makes no network call, and
    nothing here has ever held a payment key. It reproduces three documented
    behaviours of an idempotent create: the same key with the same parameters
    replays the first result, the same key with different parameters is an
    error, and a key is only remembered for a retention window, after which
    presenting it again would create a new request rather than replay the old
    one.
    """

    __tablename__ = "simulated_provider_charges"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_simulated_charge_key"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    idempotency_key: Mapped[str] = mapped_column(String(128))
    request_fingerprint: Mapped[str] = mapped_column(String(64))
    charge_ref: Mapped[str] = mapped_column(String(128))
    amount_cents: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# --------------------------------------------------------------------------- #
# Journal
# --------------------------------------------------------------------------- #

class JournalEntry(Base):
    """What was done, and above all what was refused.

    A refusal that is not written down is indistinguishable from a bug, both to
    the partner on the phone and to the engineer reading the incident a month
    later.
    """

    __tablename__ = "journal_entries"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    action: Mapped[str] = mapped_column(String(64), index=True)
    outcome: Mapped[str] = mapped_column(String(16), index=True)      # accepted, refused
    subject: Mapped[str] = mapped_column(String(160))
    reason: Mapped[str | None] = mapped_column(Text)
    detail: Mapped[dict | None] = mapped_column(JSONB)
