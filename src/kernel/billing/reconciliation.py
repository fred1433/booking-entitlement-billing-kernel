"""The reconciliation report.

Three questions, answered in one place, for one period:

* what did we charge;
* what did we decide not to charge, and why;
* what are we waiting on, from whom, and since when.

The third one is the one that is usually missing, and it is the one that makes
the difference between an integration that is late and an integration that is
late for a reason somebody can act on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..clock import utc_now
from ..models import BillingLine, Entitlement, LineState, QuarantineItem


@dataclass
class LineSummary:
    state: str
    count: int
    amount_cents: int


@dataclass
class Waiting:
    partner: str
    external_id: str | None
    kind: str
    needed_from_partner: str
    raised_at: datetime
    days_open: int


@dataclass
class Reconciliation:
    period: str
    generated_at: datetime
    by_state: list[LineSummary] = field(default_factory=list)
    waiting_on_partners: list[Waiting] = field(default_factory=list)
    entitlements_total: int = 0

    @property
    def charged_cents(self) -> int:
        return sum(row.amount_cents for row in self.by_state if row.state == LineState.CHARGED.value)

    @property
    def refund_due_cents(self) -> int:
        return sum(row.amount_cents for row in self.by_state
                   if row.state == LineState.REFUND_DUE.value)

    def count(self, state: str) -> int:
        return sum(row.count for row in self.by_state if row.state == state)


def reconcile(session: Session, period: str, now: datetime | None = None) -> Reconciliation:
    now = now or utc_now()
    rows = session.execute(
        select(BillingLine.state, func.count(BillingLine.id), func.coalesce(func.sum(BillingLine.amount_cents), 0))
        .where(BillingLine.period == period)
        .group_by(BillingLine.state)
        .order_by(BillingLine.state)
    ).all()

    open_items = session.execute(
        select(QuarantineItem)
        .where(QuarantineItem.resolved_at.is_(None))
        .order_by(QuarantineItem.raised_at)
    ).scalars().all()

    return Reconciliation(
        period=period,
        generated_at=now,
        by_state=[LineSummary(state, count, total) for state, count, total in rows],
        waiting_on_partners=[
            Waiting(
                partner=item.partner,
                external_id=item.external_id,
                kind=item.kind,
                needed_from_partner=item.needed_from_partner,
                raised_at=item.raised_at,
                days_open=max((now - item.raised_at).days, 0),
            )
            for item in open_items
        ],
        entitlements_total=session.execute(
            select(func.count(Entitlement.id))
        ).scalar_one(),
    )


def money(amount_cents: int, currency: str = "EUR") -> str:
    return f"{currency} {amount_cents // 100}.{amount_cents % 100:02d}"


def render_reconciliation(report: Reconciliation) -> str:
    lines = [
        f"reconciliation  period={report.period}  generated={report.generated_at.isoformat(timespec='seconds')}",
        "",
        "billing lines",
    ]
    if not report.by_state:
        lines.append("  none")
    for row in report.by_state:
        lines.append(f"  {row.state:<12} {row.count:>4}   {money(row.amount_cents):>14}")

    lines += ["", "waiting on partners"]
    if not report.waiting_on_partners:
        lines.append("  nothing open")
    for item in report.waiting_on_partners:
        subject = f"{item.partner}/{item.external_id}" if item.external_id else item.partner
        lines.append(f"  {subject:<28} {item.kind:<28} open {item.days_open} day(s)")
        lines.append(f"      needed: {item.needed_from_partner}")
    return "\n".join(lines)
