"""Owns the per-port recorders and live state for one recorder actor run:
loads the shared extract/State config, snapshots it next to the data, and
routes each received message to its port's :class:`~.base.Recorder`
(writing to disk) and :class:`LiveState` (in-memory, a building block for
optional in-actor plotting).
"""

import logging
import runpy
import shutil
from collections.abc import Collection
from pathlib import Path
from typing import Any

import xarray as xr
from libmuscle import Message

from muscle3_dashboard.recorder.base import (
    DeserializeFn,
    ExtractFn,
    Recorder,
    RecorderFactory,
    RecorderState,
)
from muscle3_dashboard.recorder.zarr_recorder import _combine
from muscle3_dashboard.visualization.base_state import BaseState

logger = logging.getLogger()


def load_extract_config(
    config_path: str,
    fields: Collection[str] | None = None,
    automatic_extract: bool = False,
) -> ExtractFn:
    """Load the extraction logic from a config file: either a callable
    ``extract(message) -> dict[str, Dataset]``, or a ``State`` class (a plot
    file), each message then going through a fresh instance.

    If the file's ``State`` does not implement its own ``extract`` (e.g. it
    exists only to hold data for the file's ``Plotter``), and
    ``automatic_extract`` is true, extraction falls back to
    :meth:`~muscle3_dashboard.visualization.base_state.BaseState.automatic_extract`
    instead of raising. This is opt-in so a ``State`` that simply forgot to
    implement ``extract`` still fails loudly rather than silently doing
    something else -- and it works for whatever domain-specific
    ``automatic_extract`` a subclass implements, generically.

    If ``fields`` is given, the result is restricted to those keys (as
    returned by the config's own ``extract``/``automatic_extract``) --
    for example to keep a recording focused on a handful of quantities
    instead of everything discoverable.
    """
    namespace = runpy.run_path(config_path)
    extract = namespace.get("extract")
    if extract is not None and callable(extract):
        raw_extract = extract
    else:
        state_class = namespace.get("State")
        if not (
            state_class is not None
            and isinstance(state_class, type)
            and issubclass(state_class, BaseState)
        ):
            raise NameError(
                f"{config_path} must define a callable 'extract(message)' "
                f"returning a mapping of name -> xarray.Dataset, or a "
                f"'State' class inheriting from BaseState."
            )
        if state_class.extract is BaseState.extract:
            if not automatic_extract:
                raise NameError(
                    f"{config_path}'s 'State' class does not implement "
                    f"'extract'. Either implement it, or set "
                    f"'automatic_extract: true' to fill in automatic "
                    f"extraction instead."
                )
            raw_extract = _automatic_extract_via(state_class)
        else:

            def extract_via_state(message: Any) -> dict[str, xr.Dataset]:
                state = state_class({})
                state.extract(message)
                return dict(state.data)

            raw_extract = extract_via_state

    if not fields:
        return raw_extract

    field_set = frozenset(fields)

    def filtered_extract(message: Any) -> dict[str, xr.Dataset]:
        return {k: v for k, v in raw_extract(message).items() if k in field_set}

    return filtered_extract


def _automatic_extract_via(state_class: type) -> ExtractFn:
    """An :data:`~.base.ExtractFn` that fills in extraction via a
    domain-specific ``BaseState.automatic_extract`` for a ``State`` that
    doesn't implement its own ``extract``.

    Zarr rejects ``/`` in a variable name, and ``automatic_extract``
    implementations commonly use full paths built from one (e.g. a source
    name and a field path), so any ``/`` is flattened to ``.`` first, both
    as the returned dict's keys and as each dataset's data variable name(s).
    """

    def extract(data: Any) -> dict[str, xr.Dataset]:
        state = state_class({})
        # A fresh state is built per message, so a persisted is_visualized
        # selection can't work here; extract everything discoverable
        # instead.
        state.extract_all = True
        state.automatic_extract(data)
        return {
            key.replace("/", "."): ds.rename(
                {var: str(var).replace("/", ".") for var in ds.data_vars}
            )
            for key, ds in state.data.items()
        }

    return extract


def snapshot_config(config: Path, store_path: Path) -> Path:
    """Copy the config file next to the data, so a viewer can plot the
    stores with the exact code that produced them even after the original
    is edited. Returns the copy (the original if copying failed)."""
    snapshot = store_path / config.name
    try:
        if snapshot.resolve() != config.resolve():
            shutil.copy2(config, snapshot)
        return snapshot
    except OSError:
        logger.warning(
            "could not snapshot config %s to %s",
            config,
            snapshot,
            exc_info=True,
        )
        return config


class LiveState:
    """Accumulates one port's extracted datasets in memory as they arrive --
    live-tailable like a visualization actor's ``State``. A building
    block for optional in-actor plotting (not wired up to a server yet)."""

    def __init__(self) -> None:
        self.data: dict[str, xr.Dataset] = {}

    def update(self, datasets: dict[str, xr.Dataset]) -> None:
        for name, ds in datasets.items():
            self.data[name] = (
                _combine([self.data[name], ds]) if name in self.data else ds
            )


class RecorderCollection:
    """One :class:`~.base.Recorder` and one :class:`LiveState` per
    connected port, sharing a single config file."""

    def __init__(
        self,
        store_path: Path,
        config: Path,
        deserializers: dict[str, DeserializeFn],
        make_recorder: RecorderFactory,
        fields: Collection[str] | None = None,
        automatic_extract: bool = False,
    ) -> None:
        self.extract = load_extract_config(str(config), fields, automatic_extract)
        self.config_snapshot = snapshot_config(config, store_path)
        self.live_state: dict[str, LiveState] = {
            port: LiveState() for port in deserializers
        }
        self._recorders: dict[str, Recorder] = {
            port: make_recorder(
                store_path / port,
                deserialize,
                self.extract,
                str(self.config_snapshot),
            )
            for port, deserialize in deserializers.items()
        }

    def handle(self, port: str, msg: Message) -> str:
        """Write ``msg`` via ``port``'s recorder and fold it into that
        port's live state; returns a short detail to log."""
        detail, datasets = self._recorders[port].handle(msg)
        self.live_state[port].update(datasets)
        return detail

    def close(self) -> None:
        for recorder in self._recorders.values():
            recorder.close()

    def get_state(self) -> dict[str, RecorderState]:
        """Every port's :class:`~.base.Recorder` bookkeeping, for a
        checkpoint. Live state is excluded: it's an in-memory-only building
        block for future plotting, not something a resume needs to restore."""
        return {port: rec.get_state() for port, rec in self._recorders.items()}

    def restore_state(self, state: dict[str, RecorderState]) -> None:
        """Resume every port's recorder from a previous :meth:`get_state`."""
        for port, port_state in state.items():
            self._recorders[port].restore_state(port_state)
