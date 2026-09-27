"""Demand splitting and pressure arithmetic for the steam headers."""

from typing import Mapping


def valid_header_id(header_id: str) -> str:
    """A header identifier has to be non-empty and carry no key separators."""
    text = str(header_id).strip()
    if not text or "/" in text:
        raise ValueError(f"header id {header_id!r} is not usable")
    return text


def allocate(capacity: float, demands: Mapping[str, float]) -> dict[str, float]:
    """Split the supply across the headers, pro-rata when they overdraw it."""
    capacity = float(capacity)
    total = sum(float(demand) for demand in demands.values())
    if total <= 0.0:
        return {header_id: 0.0 for header_id in demands}
    if total <= capacity:
        return {header_id: float(demand) for header_id, demand in demands.items()}
    return {
        header_id: float(demand) * capacity / total
        for header_id, demand in demands.items()
    }


def fair_share(capacity: float, count: int) -> float:
    """An even split of the supply, used to tell grabbing from being squeezed."""
    if count < 1:
        return 0.0
    return float(capacity) / count


def oversubscribed(capacity: float, total: float) -> bool:
    """Whether the headers together ask for more than the main can give."""
    return float(total) > float(capacity)


def shortfall(demand: float, granted: float) -> float:
    """Demand the split could not cover."""
    return max(0.0, float(demand) - float(granted))


def status(demand: float, share: float, contended: bool) -> str:
    """Who is grabbing steam and who is being squeezed out."""
    if not contended:
        return "satisfied"
    if float(demand) > float(share):
        return "grabbing"
    return "starved"


def offset(reading: float, baseline: float) -> float:
    """Reading measured against the calibrated baseline."""
    return float(reading) - float(baseline)


def verdict(offset_value: float, threshold: float = 0.15) -> str:
    """Whether a pressure reading sits inside the calibration tolerance."""
    if abs(float(offset_value)) <= float(threshold):
        return "in_band"
    return "out_of_band"
