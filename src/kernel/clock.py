"""Partner timestamps, and the one thing you cannot do with them.

A partner timestamp is not an instant. It is a claim about an instant, written
in whatever convention that partner uses. Three of those conventions cannot be
turned into a single instant at all:

* a naive local time inside the hour that a daylight saving change repeats
  (two instants, one hour apart, both correct);
* a naive local time inside the hour that a daylight saving change skips
  (no instant, that wall clock reading never happened);
* a timestamp with no timezone and no declared zone (nothing to resolve it
  against).

So this module does not return a single instant. It returns an interval, the
narrowest window that certainly contains the instant the partner meant. An
unambiguous timestamp gives an interval of zero width. An ambiguous one gives
an interval one hour wide.

Ordering is then decided on intervals, and only when the intervals do not
overlap. When they overlap, the kernel does not know which update is newer and
says so, instead of picking one and being quietly wrong twice a year.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from zoneinfo import ZoneInfo


class TimestampProblem(str, Enum):
    NONE = "none"
    NOT_PARSEABLE = "not_parseable"
    NONEXISTENT_LOCAL_TIME = "nonexistent_local_time"
    NO_ZONE_DECLARED = "no_zone_declared"


@dataclass(frozen=True)
class Instant:
    """The narrowest window that certainly contains one partner timestamp.

    ``lo == hi`` means the timestamp resolved to exactly one instant.
    ``lo < hi`` means the timestamp is ambiguous and the true instant is
    somewhere in the window. ``problem`` is set when the timestamp cannot be
    used at all.
    """

    raw: str
    lo: datetime | None
    hi: datetime | None
    ambiguous: bool
    problem: TimestampProblem = TimestampProblem.NONE

    @property
    def usable(self) -> bool:
        return self.problem is TimestampProblem.NONE and self.lo is not None

    @property
    def midpoint(self) -> datetime | None:
        """A single value for display and for stable sorting. Never for ordering."""
        if self.lo is None or self.hi is None:
            return None
        return self.lo + (self.hi - self.lo) / 2


class Order(str, Enum):
    NEWER = "newer"
    OLDER = "older"
    SAME = "same"
    NOT_PROVABLE = "not_provable"


def parse_partner_timestamp(raw: str, partner_timezone: str | None) -> Instant:
    """Turn one partner timestamp into an interval.

    ``partner_timezone`` is the zone the partner has told us in writing that
    its naive timestamps are written in. It is never inferred from the data.
    """
    text = (raw or "").strip()
    if not text:
        return Instant(raw=raw, lo=None, hi=None, ambiguous=False,
                       problem=TimestampProblem.NOT_PARSEABLE)

    normalised = text.replace("Z", "+00:00").replace(" ", "T", 1) if "T" not in text else text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalised)
    except ValueError:
        return Instant(raw=raw, lo=None, hi=None, ambiguous=False,
                       problem=TimestampProblem.NOT_PARSEABLE)

    if parsed.tzinfo is not None:
        exact = parsed.astimezone(timezone.utc)
        return Instant(raw=raw, lo=exact, hi=exact, ambiguous=False)

    if not partner_timezone:
        return Instant(raw=raw, lo=None, hi=None, ambiguous=False,
                       problem=TimestampProblem.NO_ZONE_DECLARED)

    zone = ZoneInfo(partner_timezone)
    first = parsed.replace(tzinfo=zone, fold=0).astimezone(timezone.utc)
    second = parsed.replace(tzinfo=zone, fold=1).astimezone(timezone.utc)

    # A wall clock reading that does not survive a round trip through UTC is a
    # reading that never happened: the clock jumped over it in spring.
    if first.astimezone(zone).replace(tzinfo=None) != parsed:
        return Instant(raw=raw, lo=None, hi=None, ambiguous=False,
                       problem=TimestampProblem.NONEXISTENT_LOCAL_TIME)

    if first == second:
        return Instant(raw=raw, lo=first, hi=first, ambiguous=False)

    # Two instants for one reading: the clock went back and this hour ran twice.
    lo, hi = sorted([first, second])
    return Instant(raw=raw, lo=lo, hi=hi, ambiguous=True)


def compare(candidate: Instant, stored: Instant) -> Order:
    """Order two partner timestamps, or refuse to.

    Strictly newer means the whole candidate window is after the whole stored
    window. Anything else that is not an exact match is not provable, and the
    kernel treats "not provable" as a reason to stop, never as a tie to break.
    """
    if not candidate.usable or not stored.usable:
        return Order.NOT_PROVABLE
    if candidate.lo == stored.lo and candidate.hi == stored.hi:
        return Order.SAME
    if candidate.lo > stored.hi:
        return Order.NEWER
    if candidate.hi < stored.lo:
        return Order.OLDER
    return Order.NOT_PROVABLE


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def month_bounds(period: str) -> tuple[datetime, datetime]:
    """Half open bounds of a billing period written as YYYY-MM, in UTC."""
    year, month = (int(part) for part in period.split("-"))
    start = datetime(year, month, 1, tzinfo=timezone.utc)
    end = datetime(year + (month == 12), (month % 12) + 1, 1, tzinfo=timezone.utc)
    return start, end


def period_of(moment: datetime) -> str:
    moment = moment.astimezone(timezone.utc)
    return f"{moment.year:04d}-{moment.month:02d}"


ONE_HOUR = timedelta(hours=1)
