"""Runs a MUSCLE3 recorder actor from start to finish, generic over the
message format: a domain package supplies only a per-port deserializer (and,
optionally, a non-Zarr :class:`~.base.Recorder`) and gets checkpoint/resume
and settings handling for free.

The recorder listens on ``F_INIT`` ports: every ``reuse_instance()``
iteration delivers one message per port, and the loop ends by itself once
the senders close their ports, using only libmuscle's public API.

The previous implementation depended on libmuscle internals (the private
``Instance._communicator`` and its end-of-stream signalling), which broke
with muscle3 0.11 and would break again on any future internal change. It
is switched off until a design based only on muscle3's public API is in
place; the old implementation is in the git history.
"""

import logging
from collections.abc import Callable
from pathlib import Path

from libmuscle import Instance, InstanceFlags, Message
from ymmsl.v0_2 import Operator

from muscle3_dashboard.muscle3_utils import get_port_list
from muscle3_dashboard.recorder.base import DeserializeFn, RecorderFactory
from muscle3_dashboard.recorder.collection import RecorderCollection
from muscle3_dashboard.recorder.zarr_recorder import ZarrRecorder

logger = logging.getLogger()


def _check_ports(instance: Instance) -> list[str]:
    """The connected F_INIT ports; fails loudly on S ports, which older
    recorder configs used and which this recorder no longer reads."""
    s_ports = get_port_list(instance, Operator.S)
    if s_ports:
        msg = (
            f"recorder ports must be f_init, not s: change {s_ports} to "
            f"f_init in the yMMSL"
        )
        instance.error_shutdown(msg)
        raise RuntimeError(msg)
    return get_port_list(instance, Operator.F_INIT)


def _check_port_dir_empty(instance: Instance, port_dir: Path) -> None:
    """A fresh run never mixes with, or deletes, earlier recordings."""
    if port_dir.exists() and any(port_dir.iterdir()):
        msg = f"{port_dir} is not empty: remove it or choose another store_path"
        instance.error_shutdown(msg)
        raise RuntimeError(msg)


def _make_collection(
    instance: Instance,
    ports: list[str],
    deserializer_for_port: Callable[[str], DeserializeFn],
    make_recorder: RecorderFactory,
    resuming: bool,
) -> RecorderCollection:
    """Read the recorder settings and build its collection over ``ports``."""
    config = Path(instance.get_setting("config", "str"))
    automatic_extract = instance.get_setting("automatic_extract", "bool", default=False)
    fields = instance.get_setting("automatic_extract_fields", "str", default="")
    record_interval = instance.get_setting("record_interval", "float", default=0.0)
    if record_interval < 0:
        raise ValueError(f"record_interval must be >= 0, got {record_interval}")
    store_path_setting = instance.get_setting("store_path", "str", default="")
    store_path = Path(store_path_setting) if store_path_setting else Path.cwd()

    if not resuming:
        for port in ports:
            _check_port_dir_empty(instance, store_path / port)
    store_path.mkdir(parents=True, exist_ok=True)

    logger.info("recording %d timeline(s) %s to %s", len(ports), ports, store_path)
    return RecorderCollection(
        store_path,
        config,
        {p: deserializer_for_port(p) for p in ports},
        make_recorder,
        fields=fields.split() or None,
        automatic_extract=automatic_extract,
        record_interval=record_interval,
    )


def _record(
    instance: Instance, collection: RecorderCollection, ports: list[str]
) -> float:
    """Receive and record this iteration's message on every port; returns
    the latest timestamp seen."""
    t_last = 0.0
    for port in ports:
        msg = instance.receive(port)
        detail = collection.handle(port, msg)
        logger.info("handled %s t=%.4e -> %s", port, msg.timestamp, detail)
        t_last = max(t_last, msg.timestamp)
    return t_last


def run_recorder_actor(
    deserializer_for_port: Callable[[str], DeserializeFn],
    make_recorder: RecorderFactory = ZarrRecorder,
) -> None:
    """Runs a recorder actor from start to finish.

    Reads its settings (``config``, ``store_path``, ``automatic_extract``,
    ``automatic_extract_fields``), then records every message on its
    ``F_INIT`` ports until the senders close them, with checkpoint/resume
    support. Each port's messages are deserialized via
    ``deserializer_for_port(port)``.

    Args:
        deserializer_for_port: Builds the :data:`~.base.DeserializeFn` for
            one port (called once, with each connected ``F_INIT`` port's
            name), so it can raise an exception up front on a port name it
            doesn't recognise, before any data arrives.
        make_recorder: The :data:`~.base.RecorderFactory` to write each
            port's timeline with. Defaults to
            :class:`~.zarr_recorder.ZarrRecorder`.
    """
    instance = Instance(flags=InstanceFlags.USES_CHECKPOINT_API)
    ports: list[str] = []
    collection: RecorderCollection | None = None
    try:
        while instance.reuse_instance():
            resuming = instance.resuming()
            snapshot = instance.load_snapshot().data if resuming else None
            should_init = instance.should_init()

            if collection is None:
                ports = _check_ports(instance)
                if not ports:
                    logger.warning(
                        "recorder has no connected F_INIT ports; nothing to record."
                    )
                    break
                collection = _make_collection(
                    instance, ports, deserializer_for_port, make_recorder, resuming
                )
            if snapshot is not None:
                collection.restore_state(snapshot)
            if not should_init:
                continue

            try:
                t_last = _record(instance, collection, ports)
            except Exception as exc:
                msg = f"recorder failed: {exc!r}"
                logger.exception(msg)
                instance.error_shutdown(msg)
                raise

            if instance.should_save_final_snapshot():
                instance.save_final_snapshot(
                    Message(t_last, data=collection.get_state())
                )
    finally:
        if collection is not None:
            collection.close()
