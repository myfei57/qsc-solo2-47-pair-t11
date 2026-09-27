"""Steam headers: per-header baselines, drain batches and the supply split.

The main only carries one pressure, so each header keeps its own calibrated
baseline with a generation; swapping a gauge moves just that header's generation
on and the old baseline can no longer serve a verdict. Every drain is reserved as
a batch in the drain registry, so the same batch can never be recorded twice, and
the demand rows show who is overdrawing the main and how the supply splits then.
"""

from typing import Any, Mapping

from ..clock import LogicalClock
from ..errors import OutOfRangeError, StaleArtifactError, UnknownKeyError
from ..event import EventBus, topics
from ..store import Journal, keys
from ..verdict import Band, BatchRegistry, VerdictHistory
from ..version import Confirmations, Generations
from . import calc
from .view import HeadersView

HEADER_NAME = "headers"
BASELINE_BAND = Band(0.5, 12.0)


def baseline_name(header_id: str) -> str:
    """Generation name of one header's pressure baseline."""
    return f"header.baseline.{header_id}"


class HeaderService:
    """Registered headers, their baselines, drain batches and the supply split."""

    def __init__(
        self,
        journal: Journal,
        bus: EventBus,
        clock: LogicalClock,
        generations: Generations,
        confirmations: Confirmations,
        drains: BatchRegistry,
    ) -> None:
        self._journal = journal
        self._bus = bus
        self._clock = clock
        self._generations = generations
        self._confirmations = confirmations
        self._drains = drains
        self._band = BASELINE_BAND
        self._histories: dict[str, VerdictHistory] = {}

    # ----------------------------------------------------------------- reading

    def header_ids(self) -> tuple[str, ...]:
        prefix = keys.HEADER_PREFIX
        return tuple(
            key[len(prefix) :]
            for key in self._journal.keys(prefix)
            if "/" not in key[len(prefix) :]
        )

    def registered(self, header_id: str) -> bool:
        return self._journal.has(keys.header_key(header_id))

    def require_header(self, header_id: str) -> Mapping[str, Any]:
        row = self._journal.read(keys.header_key(header_id))
        if row is None:
            raise UnknownKeyError(f"unknown header {header_id}")
        return row

    def demand(self, header_id: str) -> float:
        return float(self.require_header(header_id)["demand"])

    def demands(self) -> dict[str, float]:
        return {header_id: self.demand(header_id) for header_id in self.header_ids()}

    def supply(self) -> float:
        row = self._journal.read(keys.HEADERS_SUPPLY)
        if row is None:
            return 0.0
        return float(row["capacity"])

    def baseline(self, header_id: str) -> dict[str, Any] | None:
        return self._journal.read(keys.header_baseline_key(header_id))

    def generation(self, header_id: str) -> int:
        row = self.baseline(header_id)
        if row is None:
            return 0
        return int(row["generation"])

    def require_fresh_baseline(self, header_id: str) -> Mapping[str, Any]:
        """Return the baseline in force or refuse a verdict built on a stale one."""
        self.require_header(header_id)
        row = self.baseline(header_id)
        if row is None:
            raise StaleArtifactError(
                f"header {header_id} has no calibrated pressure baseline"
            )
        self._generations.require_current(
            baseline_name(header_id),
            int(row["generation"]),
            f"header {header_id} baseline",
        )
        return row

    def drains(self, header_id: str | None = None) -> tuple[Mapping[str, Any], ...]:
        """Recorded drain batches, optionally narrowed to one header."""
        if header_id is None:
            return self._drains.entries()
        self.require_header(header_id)
        return tuple(
            entry for entry in self._drains.entries() if entry["header"] == header_id
        )

    def drain_count(self, header_id: str) -> int:
        return len(self.drains(header_id))

    def allocation(self) -> dict[str, float]:
        return calc.allocate(self.supply(), self.demands())

    def fair_share(self, capacity: float | None = None) -> float:
        supply = self.supply() if capacity is None else float(capacity)
        return calc.fair_share(supply, len(self.header_ids()))

    def rows(self) -> tuple[dict[str, Any], ...]:
        """One row per header: demand, granted share, shortfall and who grabs."""
        capacity = self.supply()
        demands = self.demands()
        granted = calc.allocate(capacity, demands)
        share = calc.fair_share(capacity, len(demands))
        contended = calc.oversubscribed(capacity, sum(demands.values()))
        rows = []
        for header_id in sorted(demands, key=lambda item: (-demands[item], item)):
            baseline = self.baseline(header_id)
            rows.append(
                {
                    "header_id": header_id,
                    "demand": demands[header_id],
                    "granted": granted[header_id],
                    "shortfall": calc.shortfall(demands[header_id], granted[header_id]),
                    "status": calc.status(demands[header_id], share, contended),
                    "baseline": 0.0 if baseline is None else float(baseline["baseline"]),
                    "generation": 0 if baseline is None else int(baseline["generation"]),
                    "calibrated": baseline is not None,
                    "drains": self.drain_count(header_id),
                }
            )
        return tuple(rows)

    def history(self, header_id: str) -> VerdictHistory:
        if header_id not in self._histories:
            self._histories[header_id] = VerdictHistory(f"header.pressure.{header_id}")
        return self._histories[header_id]

    def view(self) -> HeadersView:
        demands = self.demands()
        total = sum(demands.values())
        capacity = self.supply()
        return HeadersView(
            supply=capacity,
            demand=total,
            oversubscribed=calc.oversubscribed(capacity, total),
            headers=self.rows(),
        )

    # ------------------------------------------------------------------ writes

    def register(self, header_id: str, demand: float = 0.0) -> dict[str, Any]:
        """Declare a header with its demand; redeclaring moves the demand."""
        header_id = calc.valid_header_id(header_id)
        demand = self._non_negative(demand, "header demand")
        row = {"header_id": header_id, "demand": demand, "tick": self._clock.tick}
        with self._journal.transaction("header-register") as batch:
            self._journal.append(keys.header_key(header_id), row, batch)
        self._bus.publish(topics.TOPIC_HEADER_REGISTERED, header_id)
        return dict(row)

    def set_demand(self, header_id: str, demand: float) -> dict[str, Any]:
        """Move a registered header's demand."""
        self.require_header(header_id)
        demand = self._non_negative(demand, "header demand")
        row = {"header_id": header_id, "demand": demand, "tick": self._clock.tick}
        with self._journal.transaction("header-demand") as batch:
            self._journal.append(keys.header_key(header_id), row, batch)
        self._bus.publish(topics.TOPIC_HEADER_DEMAND, header_id)
        return dict(row)

    def set_supply(self, capacity: float) -> dict[str, Any]:
        """Record what the main can currently give the headers."""
        capacity = self._non_negative(capacity, "supply capacity")
        row = {"capacity": capacity, "tick": self._clock.tick}
        with self._journal.transaction("header-supply") as batch:
            self._journal.append(keys.HEADERS_SUPPLY, row, batch)
        self._bus.publish(topics.TOPIC_HEADER_SUPPLY, capacity)
        return dict(row)

    def drain(self, header_id: str, batch_id: str, volume: float) -> dict[str, Any]:
        """Record a drain batch; the same batch is never accepted twice."""
        self.require_header(header_id)
        volume = self._non_negative(volume, "drain volume")
        self._drains.reserve(
            batch_id,
            {
                "kind": "header-drain",
                "header": header_id,
                "volume": volume,
                "tick": self._clock.tick,
            },
        )
        self._bus.publish(topics.TOPIC_HEADER_DRAINED, batch_id)
        return {
            "header_id": header_id,
            "batch_id": batch_id,
            "volume": volume,
            "drains": self.drain_count(header_id),
        }

    def calibrate(
        self, header_id: str, pressure: float, confirmation_id: str
    ) -> dict[str, Any]:
        """Adopt a header's pressure baseline under a confirmation and a generation."""
        self.require_header(header_id)
        self._band.require(pressure, f"header {header_id} baseline")
        self._confirmations.redeem(
            confirmation_id, baseline_name(header_id), HEADER_NAME
        )
        generation = self._generations.advance(
            baseline_name(header_id), "header-generation"
        )
        tick = self._clock.tick
        row = {
            "baseline": float(pressure),
            "offset": float(pressure),
            "generation": generation,
            "tick": tick,
        }
        with self._journal.transaction("header-calibrate") as batch:
            self._journal.append(
                keys.header_baseline_key(header_id), row, batch, generation=generation
            )
        self._bus.publish(topics.TOPIC_HEADER_BASELINE_SET, generation)
        return dict(row)

    def replace_gauge(self, header_id: str) -> int:
        """Move the generation on so the stored baseline can no longer be used."""
        self.require_header(header_id)
        generation = self._generations.advance(
            baseline_name(header_id), "header-gauge"
        )
        self._bus.publish(topics.TOPIC_HEADER_BASELINE_INVALIDATED, generation)
        return generation

    def reload(self) -> HeadersView:
        return self.view()

    # -------------------------------------------------------------- arithmetic

    def pressure_of(self, header_id: str, reading: float) -> str:
        value = self.offset_of(header_id, reading)
        result = calc.verdict(value)
        self.history(header_id).record(
            result,
            self._journal.watermark,
            self._clock.tick,
            f"reading={reading}",
        )
        return result

    def offset_of(self, header_id: str, reading: float) -> float:
        baseline = self.require_fresh_baseline(header_id)
        return calc.offset(reading, float(baseline["baseline"]))

    def allocate_for(self, capacity: float) -> dict[str, float]:
        return calc.allocate(capacity, self.demands())

    def shortfall(self, demand: float, granted: float) -> float:
        return calc.shortfall(demand, granted)

    def verify_band(self, value: float, label: str) -> float:
        return self._band.require(value, label)

    def window_verdict(self, offset_value: float, threshold: float) -> str:
        return calc.verdict(offset_value, threshold)

    # ---------------------------------------------------------------- internals

    def _non_negative(self, value: float, label: str) -> float:
        if float(value) < 0:
            raise OutOfRangeError(f"{label} must not be negative")
        return float(value)
