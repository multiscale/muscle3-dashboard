"""Per-port recorders for one recorder actor run.

Loads the shared extract/State config, snapshots it next to the data, and
routes each message to its port's :class:`~.base.Recorder`, which writes it
to disk.
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
from muscle3_dashboard.visualization.base_state import BaseState

logger = logging.getLogger()


def load_extract_config(
    config_path: str,
    fields: Collection[str] | None = None,
    automatic_extract: bool = False,
) -> ExtractFn:
    """Load extraction logic from a config file.

    The config file defines either a callable ``extract(message) ->
    dict[str, Dataset]``, or a ``State`` class (a plot file) -- in which
    case each message goes through a fresh ``State`` instance.

    A ``State`` that doesn't implement its own ``extract`` (e.g. it exists
    only to hold data for the file's ``Plotter``) normally raises. Passing
    ``automatic_extract=True`` opts in to falling back on
    :meth:`~.visualization.base_state.BaseState.automatic_extract` instead,
    so a ``State`` that simply forgot to implement ``extract`` still fails
    loudly by default.

    ``fields``, if given, restricts the result to those keys -- e.g. to
    keep a recording focused on a handful of quantities.
    """
    namespace = runpy.run_path(config_path)
    raw_extract = _resolve_extract_fn(namespace, config_path, automatic_extract)
    return _restrict_to_fields(raw_extract, fields)


def _resolve_extract_fn(
    namespace: dict[str, Any], config_path: str, automatic_extract: bool
) -> ExtractFn:
    """The config's own ``extract``, or one derived from its ``State``
    class (see :func:`load_extract_config`)."""
    extract = namespace.get("extract")
    if extract is not None and callable(extract):
        return extract

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
    if state_class.extract is not BaseState.extract:
        return _extract_via_state(state_class)

    if not automatic_extract:
        raise NameError(
            f"{config_path}'s 'State' class does not implement "
            f"'extract'. Either implement it, or set "
            f"'automatic_extract: true' to fill in automatic "
            f"extraction instead."
        )
    return _automatic_extract_via(state_class)


def _extract_via_state(state_class: type) -> ExtractFn:
    """An :data:`~.base.ExtractFn` that runs a message through a fresh
    ``state_class`` instance and returns what it collected."""

    def extract_via_state(message: Any) -> dict[str, xr.Dataset]:
        state = state_class({})
        state.extract(message)
        return dict(state.data)

    return extract_via_state


def _restrict_to_fields(
    raw_extract: ExtractFn, fields: Collection[str] | None
) -> ExtractFn:
    """Wrap ``raw_extract`` to keep only the given ``fields``, or return it
    unchanged if none were given."""
    if not fields:
        return raw_extract

    field_set = frozenset(fields)

    def filtered_extract(message: Any) -> dict[str, xr.Dataset]:
        return {k: v for k, v in raw_extract(message).items() if k in field_set}

    return filtered_extract


def _automatic_extract_via(state_class: type) -> ExtractFn:
    """Build an :data:`~.base.ExtractFn` from ``state_class.automatic_extract``.

    Zarr rejects ``/`` in variable names, and ``automatic_extract`` commonly
    builds paths containing one (e.g. source name + field path), so any
    ``/`` is flattened to ``.`` in both the returned keys and each dataset's
    data variable names.
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


class RecorderCollection:
    """One :class:`~.base.Recorder` per connected port, sharing a single
    config file."""

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
        """Write ``msg`` via ``port``'s recorder; returns a short detail to
        log."""
        return self._recorders[port].handle(msg)

    def close(self) -> None:
        for recorder in self._recorders.values():
            recorder.close()

    def get_state(self) -> dict[str, RecorderState]:
        """Every port's :class:`~.base.Recorder` bookkeeping, for a
        checkpoint."""
        return {port: rec.get_state() for port, rec in self._recorders.items()}

    def restore_state(self, state: dict[str, RecorderState]) -> None:
        """Resume every port's recorder from a previous :meth:`get_state`."""
        for port, port_state in state.items():
            self._recorders[port].restore_state(port_state)
