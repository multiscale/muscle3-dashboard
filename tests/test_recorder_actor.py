import pytest

from muscle3_dashboard.recorder.actor import run_recorder_actor


def test_recorder_actor_is_disabled_with_clear_message():
    with pytest.raises(RuntimeError, match="temporarily disabled"):
        run_recorder_actor(deserializer_for_port=lambda port: lambda data: data)
