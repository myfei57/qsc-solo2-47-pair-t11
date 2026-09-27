"""Uniqueness of recorded process batches."""

from typing import Any, Mapping

from ..errors import DuplicateBatchError, UnknownBatchError
from ..store import Journal, keys


class BatchRegistry:
    """Reserves batch identifiers so each process batch is accepted only once.

    The prefix scopes the registry, so drain batches and broke batches each keep
    their own namespace in the same stream.
    """

    def __init__(self, journal: Journal, prefix: str = keys.BATCH_PREFIX) -> None:
        self._journal = journal
        self._prefix = prefix

    def reserve(self, batch_id: str, payload: Mapping[str, Any]) -> None:
        """Publish a batch; a repeated identifier is refused."""
        if self.is_reserved(batch_id):
            raise DuplicateBatchError(f"batch {batch_id} was already accepted")
        row = {"batch_id": batch_id}
        row.update(payload)
        with self._journal.transaction("batch") as batch:
            self._journal.append(self._key(batch_id), row, batch)

    def payload(self, batch_id: str) -> Mapping[str, Any] | None:
        return self._journal.read(self._key(batch_id))

    def is_reserved(self, batch_id: str) -> bool:
        return self._journal.has(self._key(batch_id))

    def release(self, batch_id: str) -> None:
        """Forget a batch identifier so it can be issued again after a cancel."""
        if not self.is_reserved(batch_id):
            raise UnknownBatchError(f"batch {batch_id} was never accepted")
        with self._journal.transaction("batch-release") as batch:
            self._journal.append_tombstone(self._key(batch_id), batch, "released")

    def reserved_ids(self) -> tuple[str, ...]:
        return tuple(
            str(self._journal.read(key)["batch_id"])
            for key in self._journal.keys(self._prefix)
        )

    def entries(self) -> tuple[Mapping[str, Any], ...]:
        return tuple(self._journal.read(key) for key in self._journal.keys(self._prefix))

    # ---------------------------------------------------------------- internals

    def _key(self, batch_id: str) -> str:
        return self._prefix + batch_id
