"""Steam headers: per-point baselines, drain batches and contention.

The main steam pressure is a single figure, but the consumption points sit on
separate headers. Each header carries its own calibrated pressure baseline and
its own drain batches, so a grab for steam shows up as one point drawing past
its fair share while another starves. A drain batch is accepted only once, and
swapping a pressure gauge moves the header's generation on, which leaves the
old baseline expired until a fresh calibration lands.
"""

from typing import Any, Mapping

from ..clock import LogicalClock
from ..errors import OutOfRangeError, StaleArtifactError, UnknownKeyError
from ..event import EventBus, topics
from ..store import Journal, keys
from ..verdict import Band, BatchRegistry
from ..version import Confirmations, Generations
from . import calc
from .view import HeaderView, HeadersView

BASELINE_BAND = Band(0.5, 12.0)
RESERVED_NAMES = ("supply",)


def baseline_name(name: str) -> str:
    """Generation name of a header's pressure baseline."""
    return f"header.baseline.{name}"


class HeaderService:
    """Declared headers, their baselines, drain batches and the share-out."""

    def __init__(
        self,
        journal: Journal,
        bus: EventBus,
        clock: LogicalClock,
        generations: Generations,
        confirmations: Confirmations,
        batches: BatchRegistry,
    ) -> None:
        self._journal = journal
        self._bus = bus
        self._clock = clock
        self._generations = generations
        self._confirmations = confirmations
        self._batches = batches
        self._band = BASELINE_BAND

    # ----------------------------------------------------------------- reading

    def declared(self) -> tuple[str, ...]:
        """Names of every declared header."""
        prefix = keys.HEADER_PREFIX
        names = []
        for key in self._journal.keys(prefix):
            rest = key[len(prefix) :]
            if rest in RESERVED_NAMES or "/" in rest:
                continue
            names.append(rest)
        return tuple(names)

    def is_declared(self, name: str) -> bool:
        return self._journal.has(keys.header_key(name))

    def header(self, name: str) -> dict[str, Any]:
        """The declared row of a header; an unknown name is refused."""
        row = self._journal.read(keys.header_key(name))
        if row is None:
            raise UnknownKeyError(f"header {name} is not declared")
        return row

    def supply(self) -> float:
        row = self._journal.read(keys.HEADER_SUPPLY)
        if row is None:
            return 0.0
        return float(row["supply"])

    def baseline(self, name: str) -> dict[str, Any] | None:
        return self._journal.read(keys.header_baseline_key(name))

    def generation(self, name: str) -> int:
        row = self.baseline(name)
        if row is None:
            return 0
        return int(row["generation"])

    def calibrated(self, name: str) -> bool:
        """Whether the stored baseline still matches the generation in force."""
        row = self.baseline(name)
        if row is None:
            return False
        return self._generations.is_current(baseline_name(name), int(row["generation"]))

    def require_fresh_baseline(self, name: str) -> dict[str, Any]:
        """Return the baseline in force or refuse a verdict built on a stale one."""
        row = self.baseline(name)
        if row is None:
            raise StaleArtifactError(f"header {name} has no calibrated baseline")
        self._generations.require_current(
            baseline_name(name),
            int(row["generation"]),
            f"header {name} pressure baseline",
        )
        return row

    def drains(self, name: str) -> tuple[Mapping[str, Any], ...]:
        """Drain batches recorded for one header."""
        self.header(name)
        return tuple(entry for entry in self.drain_entries() if entry["header"] == name)

    def drain_entries(self) -> tuple[Mapping[str, Any], ...]:
        """Every recorded header drain batch."""
        return tuple(entry for entry in self._batches.entries() if "header" in entry)

    def view(self, supply: float | None = None) -> HeadersView:
        """The share-out of the supply across every declared header."""
        level = self.supply() if supply is None else float(supply)
        rows = [self.header(name) for name in self.declared()]
        shares = calc.allocation(
            level, {str(row["name"]): float(row["demand"]) for row in rows}
        )
        headers = tuple(
            HeaderView(
                name=str(row["name"]),
                demand=float(row["demand"]),
                draw=float(row["draw"]),
                share=shares[str(row["name"])],
                verdict=calc.grab_verdict(shares[str(row["name"])], float(row["draw"])),
                baseline=self._baseline_value(str(row["name"])),
                generation=self.generation(str(row["name"])),
                calibrated=self.calibrated(str(row["name"])),
                drains=len(self.drains(str(row["name"]))),
            )
            for row in rows
        )
        return HeadersView(
            supply=level,
            total_demand=sum(float(row["demand"]) for row in rows),
            total_draw=sum(float(row["draw"]) for row in rows),
            headers=headers,
        )

    # ------------------------------------------------------------------ writes

    def declare(self, name: str, demand: float) -> dict[str, Any]:
        """Declare a consumption point with its rated steam demand."""
        self._require_declarable(name)
        if float(demand) <= 0.0:
            raise OutOfRangeError(f"demand for header {name} must be positive")
        existing = self._journal.read(keys.header_key(name))
        draw = 0.0 if existing is None else float(existing["draw"])
        row = {
            "name": name,
            "demand": float(demand),
            "draw": draw,
            "tick": self._clock.tick,
        }
        with self._journal.transaction("header-declare") as batch:
            self._journal.append(keys.header_key(name), row, batch)
        self._bus.publish(topics.TOPIC_HEADER_DECLARED, name)
        return dict(row)

    def set_supply(self, supply: float) -> dict[str, Any]:
        """Record the total supply the headers share out."""
        if float(supply) < 0.0:
            raise OutOfRangeError("supply must not be negative")
        row = {"supply": float(supply), "tick": self._clock.tick}
        with self._journal.transaction("header-supply") as batch:
            self._journal.append(keys.HEADER_SUPPLY, row, batch)
        self._bus.publish(topics.TOPIC_HEADER_SUPPLY, float(supply))
        return dict(row)

    def report_draw(self, name: str, draw: float) -> dict[str, Any]:
        """Record how much steam a header is actually drawing."""
        row = dict(self.header(name))
        if float(draw) < 0.0:
            raise OutOfRangeError(f"draw for header {name} must not be negative")
        row["draw"] = float(draw)
        row["tick"] = self._clock.tick
        with self._journal.transaction("header-draw") as batch:
            self._journal.append(keys.header_key(name), row, batch)
        self._bus.publish(topics.TOPIC_HEADER_DRAW, name)
        return dict(row)

    def drain(self, name: str, batch_id: str, volume: float = 0.0) -> dict[str, Any]:
        """Record a drain batch for a header; a repeated batch is refused."""
        self.header(name)
        if float(volume) < 0.0:
            raise OutOfRangeError("drain volume must not be negative")
        self._batches.reserve(
            batch_id,
            {
                "header": name,
                "volume": float(volume),
                "tick": self._clock.tick,
            },
        )
        self._bus.publish(topics.TOPIC_HEADER_DRAINED, batch_id)
        return {"batch_id": batch_id, "header": name, "drains": len(self.drains(name))}

    def calibrate(
        self, name: str, pressure: float, confirmation_id: str
    ) -> dict[str, Any]:
        """Adopt a new pressure baseline for a header under a confirmation."""
        self.header(name)
        self._band.require(pressure, f"header {name} baseline")
        self._confirmations.redeem(confirmation_id, baseline_name(name), name)
        generation = self._generations.advance(baseline_name(name), "header-generation")
        tick = self._clock.tick
        row = {
            "baseline": float(pressure),
            "generation": generation,
            "tick": tick,
        }
        with self._journal.transaction("header-calibrate") as batch:
            self._journal.append(
                keys.header_baseline_key(name), row, batch, generation=generation
            )
        self._bus.publish(topics.TOPIC_HEADER_BASELINE_SET, generation)
        return dict(row)

    def replace_gauge(self, name: str) -> int:
        """Move the generation on so the stored baseline can no longer be used."""
        self.header(name)
        generation = self._generations.advance(baseline_name(name), "header-gauge")
        self._bus.publish(topics.TOPIC_HEADER_BASELINE_INVALIDATED, generation)
        return generation

    def reload(self) -> HeadersView:
        return self.view()

    # -------------------------------------------------------------- arithmetic

    def fair_share(self, supply: float, demand: float, total_demand: float) -> float:
        return calc.fair_share(supply, demand, total_demand)

    def allocation(self, supply: float, demands: Mapping[str, float]) -> dict[str, float]:
        return calc.allocation(supply, demands)

    def grab_verdict(self, share: float, draw: float) -> str:
        return calc.grab_verdict(share, draw)

    def pressure_deficit(self, supply: float, header: float) -> float:
        return calc.pressure_deficit(supply, header)

    # ---------------------------------------------------------------- internals

    def _baseline_value(self, name: str) -> float:
        row = self.baseline(name)
        if row is None:
            return 0.0
        return float(row["baseline"])

    def _require_declarable(self, name: str) -> None:
        if not name or "/" in name or name in RESERVED_NAMES:
            raise ValueError(f"header name {name!r} is not declarable")
