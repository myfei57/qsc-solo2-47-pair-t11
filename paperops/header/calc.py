"""Steam sharing arithmetic across headers."""

from typing import Mapping

GRAB_TOLERANCE = 0.05


def fair_share(supply: float, demand: float, total_demand: float) -> float:
    """The part of the supply a header may claim for its rated demand."""
    total = float(total_demand)
    if total <= 0.0:
        return 0.0
    return float(supply) * float(demand) / total


def allocation(supply: float, demands: Mapping[str, float]) -> dict[str, float]:
    """Split a supply across headers in proportion to their rated demands."""
    total = sum(float(demand) for demand in demands.values())
    return {name: fair_share(supply, demand, total) for name, demand in demands.items()}


def grab_verdict(share: float, draw: float, tolerance: float = GRAB_TOLERANCE) -> str:
    """Whether a header draws past its share or is starved by the others."""
    margin = abs(float(share)) * float(tolerance)
    if float(draw) > float(share) + margin:
        return "grabbing"
    if float(draw) < float(share) - margin:
        return "starved"
    return "balanced"


def pressure_deficit(supply: float, header: float) -> float:
    """Pressure a header loses against the supply main."""
    return float(supply) - float(header)
