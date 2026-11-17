"""MUSCLE3 recorder actor -- temporarily disabled.

The previous implementation depended on libmuscle internals (the private
``Instance._communicator`` and its end-of-stream signalling), which broke
with muscle3 0.11 and would break again on any future internal change. It
is switched off until a design based only on muscle3's public API is in
place; the old implementation is in the git history.

Recording itself does not need MUSCLE3: feed messages (anything with
``timestamp``, ``next_timestamp`` and ``data``, e.g.
:class:`~.base.Record`) to a :class:`~.collection.RecorderCollection` from
your own code.
"""

from collections.abc import Callable

from muscle3_dashboard.recorder.base import DeserializeFn, RecorderFactory

DISABLED_MESSAGE = (
    "The muscle3-dashboard recorder actor is temporarily disabled: it relied "
    "on MUSCLE3 internals that changed in muscle3 0.11. Remove the recorder "
    "component from your yMMSL, or record from your own code with "
    "muscle3_dashboard.recorder.collection.RecorderCollection."
)


def run_recorder_actor(
    deserializer_for_port: Callable[[str], DeserializeFn],
    make_recorder: RecorderFactory | None = None,
) -> None:
    """Disabled; always raises. Kept so existing imports keep working and
    fail with a clear message only when the actor is actually started."""
    raise RuntimeError(DISABLED_MESSAGE)
