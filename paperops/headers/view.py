"""Read model for the steam headers."""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class HeadersView:
    supply: float
    demand: float
    oversubscribed: bool
    headers: tuple[dict[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "supply": self.supply,
            "demand": self.demand,
            "oversubscribed": self.oversubscribed,
            "headers": [dict(row) for row in self.headers],
        }
