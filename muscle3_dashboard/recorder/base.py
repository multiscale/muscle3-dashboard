"""Recorder base class: one instance per connected port.

It deserializes and extracts each received message using the shared config
logic (see :mod:`.collection`), then writes it to disk -- one store per
outer-loop iteration, rolling over to a new occurrence on a stream restart
(a message with no ``next_timestamp``, or time stepping backwards). The
on-disk format itself is left to a subclass, e.g.
:class:`~.zarr_recorder.ZarrRecorder`.
"""

from abc import ABC, abstractmethod
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypedDict

import xarray as xr
from libmuscle import Message

#: Turns a message's raw bytes payload into whatever `ExtractFn` expects
#: (e.g. a domain-specific message object, deserialized against the port's
#: schema).
DeserializeFn = Callable[[bytes], Any]

#: Maps one deserialized message to ``name -> Dataset``; every dataset
#: carries a ``time`` dimension (one instant, or a whole trace) to append
#: along.
ExtractFn = Callable[[Any], dict[str, xr.Dataset]]


class RecorderState(TypedDict):
    """A :class:`Recorder`'s bookkeeping, as saved/restored across a
    checkpoint (see :meth:`Recorder.get_state`)."""

    occurrence: int
    last_time: float | None
    prev_ended: bool
    is_open: bool


class Recorder(ABC):
    """Extracts and writes one port's timeline to disk."""

    def __init__(
        self,
        store_dir: Path,
        deserialize: DeserializeFn,
        extract: ExtractFn,
        profile: str,
    ) -> None:
        self._store_dir = store_dir
        self._deserialize = deserialize
        self._extract = extract
        self._profile = profile
        self._state: RecorderState = {
            "occurrence": 0,
            "last_time": None,
            "prev_ended": False,
            "is_open": False,
        }

    def handle(self, msg: Message) -> str:
        """Extract and write one message. Returns a short detail to log."""
        state = self._state
        restarted = state["prev_ended"] or (
            state["last_time"] is not None and msg.timestamp < state["last_time"]
        )
        if state["is_open"] and restarted:
            self._close_occurrence()
            state["occurrence"] += 1
            state["is_open"] = False
        if not state["is_open"]:
            self._open_occurrence(self._store_dir / f"{state['occurrence']:04d}")
            state["is_open"] = True

        message = self._deserialize(msg.data)
        datasets = self._extract(message)
        detail = self._write(datasets)
        state["last_time"] = msg.timestamp
        state["prev_ended"] = msg.next_timestamp is None
        return detail

    def close(self) -> None:
        """Finalize the currently open occurrence, if any (an empty
        timeline never opened one)."""
        if self._state["is_open"]:
            self._close_occurrence()

    def get_state(self) -> RecorderState:
        """Bookkeeping needed to resume this port's timeline after a
        checkpoint restart. Whatever is already durably on disk is left
        there; a subclass with extra in-memory state rehydrates it in
        :meth:`_reopen_occurrence` instead of duplicating it here."""
        return self._state.copy()

    def restore_state(self, state: RecorderState) -> None:
        """Resume from a previous :meth:`get_state`: restores bookkeeping
        and, if an occurrence was still open at checkpoint time, reopens
        it."""
        self._state = state.copy()
        if self._state["is_open"]:
            self._reopen_occurrence(
                self._store_dir / f"{self._state['occurrence']:04d}"
            )

    @abstractmethod
    def _open_occurrence(self, base: Path) -> None:
        """Open a fresh store at ``base`` (no suffix) for a new occurrence."""

    @abstractmethod
    def _write(self, datasets: dict[str, xr.Dataset]) -> str:
        """Write one message's extracted datasets; return a short detail."""

    @abstractmethod
    def _close_occurrence(self) -> None:
        """Finalize the currently open occurrence's store."""

    def _reopen_occurrence(self, base: Path) -> None:
        """Resume an occurrence that was already open at checkpoint time.
        Default: open fresh, there is no extra in-memory state to
        rehydrate; override when a subclass keeps such state (derived from
        what's already on disk)."""
        self._open_occurrence(base)


#: Builds a Recorder for one port's store dir, its message deserializer, the
#: shared extract function, and the snapshotted config path (for provenance
#: stamping).
RecorderFactory = Callable[[Path, DeserializeFn, ExtractFn, str], "Recorder"]
