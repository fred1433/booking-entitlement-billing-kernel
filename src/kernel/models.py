"""The schema.

Five guarantees in this kernel are enforced by the database, not by the service
layer, because the service layer is the part a future change can walk around:

1. one booking per (partner, external_id)                  unique index
2. one application per partner event id                    unique index
3. at most three shares per entitlement                    unique index plus check
4. one billing line per (entitlement, period)              unique index
5. one charge per idempotency key                          unique index

``tests/test_database_guarantees.py`` proves each one by trying to break it in
raw SQL, with the service layer out of the way.
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

from .config import MAX_SHARES_PER_ENTITLEMENT


class Base(DeclarativeBase):
    pass


# --------------------------------------------------------------------------- #
# Vocabularies
# --------------------------------------------------------------------------- #

class DeliveryOutcome(str, Enum):
    APPLIED = "applied"
    DUPLICATE = "duplicate"          # same event, delivered again
    SUPERSEDED = "superseded"        # provably older than what we hold
    CONFLICT = "conflict"            # same timestamp, different content
    ORDER_NOT_PROVABLE = "order_not_provable"
    QUARANTINED = "quarantined"      # unusable as received
    HELD = "held"                    # part of a batch that was not applied


class BookingStatus(str, Enum):
    CONFIRMED = "confirmed"
    AMENDED = "amended"
    CANCELLED = "cancelled"


class EntitlementState(str, Enum):
    CREATED = "created"
    CLAIMABLE = "claimable"
    CLAIMED = "claimed"
    ACTIVE = "active"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class LineState(str, Enum):
    PREPARED = "prepared"
    CHARGED = "charged"
    VOIDED = "voided"
    REFUND_DUE = "refund_due"
    BLOCKED = "blocked"              # source data is quarantined, do not bill


class BatchState(str, Enum):
    APPLIED = "applied"
    REJECTED_INCOMPLETE = "rejected_incomplete"
    HELD_FOR_REVIEW = "held_for_review"


# --------------------------------------------------------------------------- #
# Intake
# --------------------------------------------------------------------------- #

class Delivery(Base):
    """Every message the kernel received, with what it did about it.

    Append only. A duplicate is written here too: the journal answers "did you
    get my message" before it answers "did you act on it", and those are
    different questions when a partner is asking.
    """

    __tablename__ = "deliveries"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    partner: Mapped[str] = mapped_column(String(64), index=True)
    channel: Mapped[str] = mapped_column(String(16))          # push, pull, file
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
    """The kernel's own record of a booking, in the kernel's own vocabulary."""

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
    unit_amount_cents: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3))

    updated_at_raw: Mapped[str] = mapped_column(String(64))
    updated_at_lo: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at_hi: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at_ambiguous: Mapped[bool] = mapped_column(Boolean, default=False)
    payload_fingerprint: Mapped[str] = mapped_column(String(64))

    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_applied_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_delivery_id: Mapped[int | None] = mapped_column(BigInteger)

    entitlement: Mapped["Entitlement"] = relationship(back_populates="booking", uselist=False)


class QuarantineItem(Base):
    """What we are waiting on, from whom, and since when.

    This table is the blocked list an integration engineer takes into the
    partner call. Nothing is ever silently dropped: it is quarantined, and the
    reason is written in the partner's own terms.
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
# Entitlements
# --------------------------------------------------------------------------- #

class Entitlement(Base):
    __tablename__ = "entitlements"
    __table_args__ = (
        CheckConstraint(
            "state in ('created','claimable','claimed','active','cancelled','expired')",
            name="ck_entitlement_state",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    booking_id: Mapped[int] = mapped_column(ForeignKey("bookings.id"), unique=True)
    state: Mapped[str] = mapped_column(String(32), index=True)
    holder_ref: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    claimable_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_reason: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    billable_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    billable_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    booking: Mapped[Booking] = relationship(back_populates="entitlement")
    shares: Mapped[list["EntitlementShare"]] = relationship(back_populates="entitlement")


class EntitlementShare(Base):
    """Bounded sharing.

    The bound is a unique index on the share slot plus a check on the slot
    number, so the fourth share cannot be created even by two requests racing
    each other. Counting rows in Python and comparing to a limit loses that
    race roughly as often as the product is popular.
    """

    __tablename__ = "entitlement_shares"
    __table_args__ = (
        UniqueConstraint("entitlement_id", "share_index", name="uq_share_slot"),
        CheckConstraint(
            f"share_index >= 1 and share_index <= {MAX_SHARES_PER_ENTITLEMENT}",
            name="ck_share_index_within_limit",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    entitlement_id: Mapped[int] = mapped_column(ForeignKey("entitlements.id"))
    share_index: Mapped[int] = mapped_column(Integer)
    shared_with: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    entitlement: Mapped[Entitlement] = relationship(back_populates="shares")


class ClaimToken(Base):
    """A passwordless claim link, stored as a hash.

    The kernel keeps the hash, never the token. A dump of this table is not a
    set of working claim links.
    """

    __tablename__ = "claim_tokens"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    entitlement_id: Mapped[int] = mapped_column(ForeignKey("entitlements.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consumed_by: Mapped[str | None] = mapped_column(String(128))


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
    """One line per entitlement per period. The database says so.

    ``idempotency_key`` is derived, not random: the same entitlement and the
    same period always produce the same key, so a retry after a lost response
    reaches the payment provider as the same request rather than a second one.
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
    state: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str | None] = mapped_column(Text)
    charge_ref: Mapped[str | None] = mapped_column(String(128))
    prepared_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    charged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# --------------------------------------------------------------------------- #
# Journal
# --------------------------------------------------------------------------- #

class JournalEntry(Base):
    """What the kernel did, and above all what it refused to do.

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
