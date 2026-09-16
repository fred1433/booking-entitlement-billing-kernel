"""What an adapter has to be able to do, and what it is never allowed to do.

An adapter maps one partner's words onto the kernel's words. It is allowed to
translate anything it has been told about in writing. It is not allowed to
guess: an unknown status, an unknown column spelling or an unreadable timestamp
comes back as a Rejection carrying the sentence to send to the partner, never
as a default value.

File shape lives here too, next to the rest of what we know about that partner,
rather than in the intake service. The service is the same for everyone; only
adapters are allowed to know that this feed renamed a column last spring.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from ..intake.schemas import CanonicalBooking, Rejection


class PartnerAdapter(Protocol):
    name: str
    display_name: str
    #: The zone the partner has told us, in writing, that its naive timestamps
    #: are written in. None means the partner always sends an offset.
    declared_timezone: str | None

    def normalise(self, raw: dict) -> "CanonicalBooking | Rejection":
        ...

    def resolve_columns(self, header: list[str]) -> tuple[dict[str, str], list[str]]:
        """Map each canonical column onto the spelling this file uses.

        Returns the mapping and the list of canonical columns for which no
        declared spelling was found. A non empty second element stops the file.
        """
        ...
