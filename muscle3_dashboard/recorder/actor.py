"""A complete MUSCLE3 recorder-actor ``main()``, generic over the message
format: a domain package supplies only a per-port deserializer (and,
optionally, a non-Zarr :class:`~.base.Recorder`) and gets checkpoint/resume
and settings handling for free.

The recorder listens on ``F_INIT`` ports: every ``reuse_instance()``
iteration delivers one message per port, and the loop ends by itself once
the senders close their ports, using only libmuscle's public API.

See the README's "Recording your own actor's data" section for a worked
example.
"""

import logging
import shutil
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


def _make_collection(
    instance: Instance,
    ports: list[str],
    deserializer_for_port: Callable[[str], DeserializeFn],
    make_recorder: RecorderFactory,
    resuming: bool,
) -> RecorderCollection:
    """Read the recorder settings and build its collection over ``ports``."""
    config = Path(str(instance.get_setting("config", "str")))
    automatic_extract = instance.get_setting("automatic_extract", "bool", default=False)
    fields = str(instance.get_setting("automatic_extract_fields", "str", default=""))
    store_path_setting = str(instance.get_setting("store_path", "str", default=""))
    store_path = Path(store_path_setting) if store_path_setting else Path.cwd()

    store_path.mkdir(parents=True, exist_ok=True)
    if not resuming:
        # A fresh run starts clean; a resumed one keeps what's already on
        # disk. Never remove store_path itself (may be the run folder).
        for port in ports:
            shutil.rmtree(store_path / port, ignore_errors=True)

    logger.info("recording %d timeline(s) %s to %s", len(ports), ports, store_path)
    return RecorderCollection(
        store_path,
        config,
        {p: deserializer_for_port(p) for p in ports},
        make_recorder,
        fields=fields.split() or None,
        automatic_extract=automatic_extract,
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
    """A complete MUSCLE3 recorder-actor ``main()``.

    Reads the recorder actor's settings (``config``, ``store_path``,
    ``automatic_extract``, ``automatic_extract_fields``),
    builds a :class:`~.collection.RecorderCollection` over every connected
    ``F_INIT`` port -- each deserialized via ``deserializer_for_port(port)``
    -- and records one message per port per ``reuse_instance()`` iteration
    until the senders close their ports, with checkpoint/resume support.

    All ports advance in lockstep, so they must receive the same number of
    messages; record senders that run at different rates with one recorder
    each.

    Args:
        deserializer_for_port: Builds the :data:`~.base.DeserializeFn` for
            one port (called once, with each connected ``F_INIT`` port's
            name), so it can fail loudly up front on a port name it doesn't
            recognise, before any data arrives.
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
