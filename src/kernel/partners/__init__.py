"""Two fictional partners.

Neither Coralbay Reservations nor Lindhoff Booking Suite exists. They are
invented for this repository, and they are invented to disagree: one pushes
JSON with real offsets and retries a lot, the other drops a nightly file full
of naive local timestamps, its own status words, and a column that was renamed
between two file versions. Every real integration is somewhere between them.
"""

from .base import PartnerAdapter
from .coralbay import CORALBAY, CoralbayAdapter
from .lindhoff import LINDHOFF, LindhoffAdapter

__all__ = [
    "PartnerAdapter",
    "CORALBAY",
    "CoralbayAdapter",
    "LINDHOFF",
    "LindhoffAdapter",
]
