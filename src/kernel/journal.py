"""The journal.

Every refusal is written here with the reason in the same words the partner
would use. The demonstration script prints this table, and a refusal is the
interesting line, not the noise.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from .models import JournalEntry

ACCEPTED = "accepted"
REFUSED = "refused"


def record(
    session: Session,
    action: str,
    outcome: str,
    subject: str,
    reason: str | None = None,
    detail: dict | None = None,
) -> JournalEntry:
    entry = JournalEntry(
        action=action,
        outcome=outcome,
        subject=subject,
        reason=reason,
        detail=detail,
    )
    session.add(entry)
    session.flush()
    return entry


def accepted(session: Session, action: str, subject: str, reason: str | None = None,
             detail: dict | None = None) -> JournalEntry:
    return record(session, action, ACCEPTED, subject, reason, detail)


def refused(session: Session, action: str, subject: str, reason: str,
            detail: dict | None = None) -> JournalEntry:
    return record(session, action, REFUSED, subject, reason, detail)
