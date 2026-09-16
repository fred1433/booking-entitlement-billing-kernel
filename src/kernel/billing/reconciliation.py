"""The report a person actually acts on.

Three questions, in the order somebody asks them after a run:

1. what state is every line in, and for how much;
2. what is not decided, why, and what would decide it;
3. what is being waited on, from whom, and what it is holding up.

The third section is the one that is usually kept in somebody's head or a
spreadsheet. It is generated here because a blocked list that is not generated
is a blocked list that is out of date by the second week.

Every follow up line below is marked as a synthetic example. Nothing in this
repository has been sent to anybody, and no date in it is a real date of a real
request.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..clock import utc_now
from ..models import (
    BillingLine,
    Booking,
    Cancellation,
    Entitlement,
    QuarantineItem,
    VersionConflict,
)

#: What each line state means for money, in one sentence, for the report.
MEANING = {
    "prepared": "recorded, nothing submitted",
    "submitted": "submitted, outcome not yet established",
    "charged": "outcome established with the external system",
    "voided": "cancelled before anything was submitted",
    "blocked": "not prepared, a question about the source data is open",
    "unresolved": "outcome unknown and no longer safely retryable",
}


@dataclass
class OpenQuestion:
    partner: str
    external_id: str | None
    kind: str
    needed: str
    raised_at: datetime
    holding_up: str


@dataclass
class Reconciliation:
    period: str
    generated_at: datetime
    by_state: dict[str, tuple[int, int]] = field(default_factory=dict)
    cancellations: dict[str, int] = field(default_factory=dict)
    questions: list[OpenQuestion] = field(default_factory=list)


def reconcile(session: Session, period: str, now: datetime | None = None) -> Reconciliation:
    now = now or utc_now()
    report = Reconciliation(period=period, generated_at=now)

    for line in session.scalars(select(BillingLine).where(BillingLine.period == period)):
        count, amount = report.by_state.get(line.state, (0, 0))
        report.by_state[line.state] = (count + 1, amount + line.amount_cents)
        if line.cancellation != Cancellation.NONE.value:
            report.cancellations[line.cancellation] = report.cancellations.get(line.cancellation, 0) + 1

    blocked_ids = {
        booking.partner + "/" + booking.external_id
        for booking in session.scalars(select(Booking).where(Booking.undecided_reason.isnot(None)))
    }

    for item in session.scalars(
        select(QuarantineItem).where(QuarantineItem.resolved_at.is_(None))
    ):
        key = f"{item.partner}/{item.external_id}"
        report.questions.append(
            OpenQuestion(
                partner=item.partner,
                external_id=item.external_id,
                kind=item.kind,
                needed=item.needed_from_partner,
                raised_at=item.raised_at,
                holding_up=(
                    "billing preparation for this booking"
                    if key in blocked_ids
                    else "this message only, the booking is unaffected"
                ),
            )
        )

    return report


def unresolved_conflicts(session: Session) -> list[VersionConflict]:
    return list(
        session.scalars(select(VersionConflict).where(VersionConflict.resolved_at.is_(None)))
    )


def render_reconciliation(session: Session, report: Reconciliation) -> str:
    lines: list[str] = []
    stamp = report.generated_at.isoformat(timespec="seconds")
    lines.append(f"reconciliation  period={report.period}  generated={stamp}")
    lines.append("")
    lines.append("billing lines")
    if not report.by_state:
        lines.append("  none")
    for state, (count, amount) in sorted(report.by_state.items()):
        meaning = MEANING.get(state, "")
        lines.append(f"  {state:<12} {count:>3}   {amount / 100:>10,.2f}   {meaning}")

    if report.cancellations:
        lines.append("")
        lines.append("cancellations, by where they landed")
        for where, count in sorted(report.cancellations.items()):
            lines.append(f"  {where:<20} {count:>3}")

    lines.append("")
    lines.append("waiting on partners   (synthetic examples, nothing was sent to anybody)")
    if not report.questions:
        lines.append("  nothing open")
    for question in report.questions:
        age = (report.generated_at - question.raised_at).days
        lines.append(f"  {question.partner}/{question.external_id}   {question.kind}   open {age} day(s)")
        lines.append(f"      needed:      {question.needed}")
        lines.append(f"      holding up:  {question.holding_up}")
        lines.append("      counterpart: the partner's integration contact")
        lines.append("      next action: ask, then replay the held message")
        lines.append("      status:      Not requested, synthetic example")

    return "\n".join(lines)


def billable_entitlements(session: Session) -> list[Entitlement]:
    return list(session.scalars(select(Entitlement)))
