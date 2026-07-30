"""A complete MUSCLE3 recorder-actor ``main()``, generic over the message
format: a domain package supplies only a per-port deserializer (and,
optionally, a non-Zarr :class:`~.base.Recorder`) and gets checkpoint/resume,
multi-port round-robin draining, and settings handling for free.

See the README's "Recording your own actor's data" section for a worked
example.
"""

import logging
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Optional

from libmuscle import Instance, InstanceFlags, Message
from libmuscle.mpp_message import ClosePort
from ymmsl.v0_2 import Operator

from muscle3_dashboard.muscle3_utils import get_port_list, get_setting_optional
from muscle3_dashboard.recorder.base import DeserializeFn, RecorderFactory, RecorderState
from muscle3_dashboard.recorder.collection import RecorderCollection
from muscle3_dashboard.recorder.zarr_recorder import ZarrRecorder

logger = logging.getLogger()


def _serve(
    instance: Instance, collection: RecorderCollection, ports: list[str]
) -> dict[str, BaseException]:
    """Drain every timeline in turn on this thread, round-robin. Senders
    are assumed to keep pace with each other (as in a normal lockstep
    workflow), so blocking on one port in turn doesn't stall the others.
    A timeline ends when its peer's port closes; ``next_timestamp is None``
    is a stream restart (an outer-loop iteration boundary), not the end.

    After each full sweep over the still-active ports, checkpoints once
    every port has advanced past the last checkpoint, using the minimum
    timestamp across them -- so a resume never finds one port ahead of
    another's saved state.

    Returns port -> exception for failed timelines (empty on success);
    the collection is always closed.
    """
    errors: dict[str, BaseException] = {}
    active = list(ports)
    count = {p: 0 for p in active}
    last_time: dict[str, float] = {}
    try:
        while active:
            for port in list(active):
                try:
                    msg, _ = instance._communicator.receive_message(port)
                except (RuntimeError, OSError) as exc:
                    # Peer crashed mid-stream; end this timeline (data so
                    # far is on disk), let the others carry on.
                    active.remove(port)
                    logger.warning(
                        "timeline '%s' ended after %d messages: %r",
                        port,
                        count[port],
                        exc,
                    )
                    continue
                if isinstance(msg.data, ClosePort):
                    active.remove(port)
                    logger.info(
                        "timeline '%s' closed after %d messages",
                        port,
                        count[port],
                    )
                    continue
                try:
                    detail = collection.handle(port, msg)
                except BaseException as exc:  # noqa: B036 -- surfaced to caller
                    errors[port] = exc
                    logger.exception(
                        "timeline '%s' failed after %d messages",
                        port,
                        count[port],
                    )
                    active.remove(port)
                    continue
                last_time[port] = msg.timestamp
                count[port] += 1
                logger.info(
                    "handled %s t=%.4e -> %s", port, msg.timestamp, detail
                )

            if active and all(port in last_time for port in active):
                t_cur = min(last_time[port] for port in active)
                if instance.should_save_snapshot(t_cur):
                    instance.save_snapshot(
                        Message(t_cur, data=collection.get_state())
                    )
    finally:
        collection.close()
    return errors


def run_recorder_actor(
    deserializer_for_port: Callable[[str], DeserializeFn],
    make_recorder: RecorderFactory = ZarrRecorder,
) -> None:
    """A complete MUSCLE3 recorder-actor ``main()``.

    Reads the same settings the recorder actor always has (``config``,
    ``store_path``, ``automatic_extract``, ``automatic_extract_fields``),
    builds a :class:`~.collection.RecorderCollection` over every connected
    ``S`` port -- each deserialized via ``deserializer_for_port(port)`` --
    and drains it with checkpoint/resume support, reporting failures back
    to the instance.

    Args:
        deserializer_for_port: Builds the :data:`~.base.DeserializeFn` for
            one port (called once per ``reuse_instance()`` with each
            connected ``S`` port's name), so it can fail loudly up front on
            a port name it doesn't recognise, before any data arrives.
        make_recorder: The :data:`~.base.RecorderFactory` to write each
            port's timeline with. Defaults to
            :class:`~.zarr_recorder.ZarrRecorder`.
    """
    instance = Instance(flags=InstanceFlags.USES_CHECKPOINT_API)

    while instance.reuse_instance():
        s_ports = get_port_list(instance, Operator.S)
        # Validate all port -> deserializer mappings up front so a bad
        # config fails before any data is received.
        deserializers = {p: deserializer_for_port(p) for p in s_ports}

        resuming = instance.resuming()
        snapshot_state: Optional[dict[str, RecorderState]] = None
        if resuming:
            snapshot_state = instance.load_snapshot().data
        # libmuscle's checkpoint API guard requires should_init() to be
        # called right after resuming(), even though its result is unused
        # here (there is nothing to initialize beyond the snapshot above).
        instance.should_init()

        if not s_ports:
            logger.warning(
                "recorder has no connected S ports; nothing to record."
            )
            break

        config = Path(str(instance.get_setting("config", "str")))
        automatic_extract = instance.get_setting(
            "automatic_extract", "bool", default=False
        )
        fields_setting = get_setting_optional(
            instance, "automatic_extract_fields"
        )
        automatic_extract_fields = (
            str(fields_setting).split() if fields_setting else None
        )
        store_path_setting = get_setting_optional(instance, "store_path")
        store_path = (
            Path(str(store_path_setting))
            if store_path_setting is not None
            else Path.cwd()
        )

        store_path.mkdir(parents=True, exist_ok=True)
        if not resuming:
            # A fresh run starts clean; a resumed one keeps what's already
            # on disk. Never remove store_path itself (may be the run
            # folder).
            for port in s_ports:
                shutil.rmtree(store_path / port, ignore_errors=True)

        collection = RecorderCollection(
            store_path,
            config,
            deserializers,
            make_recorder,
            fields=automatic_extract_fields,
            automatic_extract=automatic_extract,
        )
        if snapshot_state is not None:
            collection.restore_state(snapshot_state)

        logger.info(
            "recording %d timeline(s) %s to %s",
            len(s_ports),
            s_ports,
            store_path,
        )
        errors = _serve(instance, collection, s_ports)
        if errors:
            msg = "; ".join(f"{port}: {exc!r}" for port, exc in errors.items())
            instance.error_shutdown(f"recorder timeline(s) failed: {msg}")
            raise RuntimeError(f"recorder timeline(s) failed: {msg}")

        if instance.should_save_final_snapshot():
            state = collection.get_state()
            all_times = (s["last_time"] for s in state.values())
            last_times = [t for t in all_times if t is not None]
            final_t = max(last_times) if last_times else 0.0
            instance.save_final_snapshot(Message(final_t, data=state))
