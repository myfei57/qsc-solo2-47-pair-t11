"""Read model for the steam headers."""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class HeaderView:
    name: str
    demand: float
    draw: float
    share: float
    verdict: str
    baseline: float
    generation: int
    calibrated: bool
    drains: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "demand": self.demand,
            "draw": self.draw,
            "share": self.share,
            "verdict": self.verdict,
            "baseline": self.baseline,
            "generation": self.generation,
            "calibrated": self.calibrated,
            "drains": self.drains,
        }


@dataclass(frozen=True)
class HeadersView:
    supply: float
    total_demand: float
    total_draw: float
    headers: tuple[HeaderView, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "supply": self.supply,
            "total_demand": self.total_demand,
            "total_draw": self.total_draw,
            "headers": [header.to_dict() for header in self.headers],
        }
