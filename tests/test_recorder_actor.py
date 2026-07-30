from unittest.mock import Mock

import pytest
from libmuscle import Message
from libmuscle.mpp_message import ClosePort

from muscle3_dashboard.recorder.actor import _serve, run_recorder_actor

# --- _serve: round-robin draining, independent of any real Instance --------


class _FakeCollection:
    """Stands in for RecorderCollection: `_serve` only ever calls these
    three methods on it."""

    def __init__(self, fail_on=None):
        self.handled: list[tuple[str, Message]] = []
        self.closed = False
        self._fail_on = fail_on or set()

    def handle(self, port, msg):
        if port in self._fail_on:
            raise ValueError(f"boom on {port}")
        self.handled.append((port, msg))
        return "ok"

    def get_state(self):
        return {"handled": len(self.handled)}

    def close(self):
        self.closed = True


def _instance(messages_by_port, checkpoint_every=None):
    """A Mock whose `_communicator.receive_message(port)` pops the next
    queued message for that port, and whose `should_save_snapshot` fires
    once `checkpoint_every` messages have been handled per port."""
    instance = Mock()
    queues = {p: list(msgs) for p, msgs in messages_by_port.items()}

    def receive_message(port):
        return queues[port].pop(0), None

    instance._communicator.receive_message.side_effect = receive_message
    instance.should_save_snapshot.return_value = checkpoint_every is not None
    return instance


def test_serve_drains_two_ports_until_closed():
    instance = _instance(
        {
            "a": [
                Message(0.0, 1.0, data="d0"),
                Message(1.0, None, data="d1"),
                Message(0.0, None, data=ClosePort()),
            ],
            "b": [
                Message(0.0, None, data="d0"),
                Message(0.0, None, data=ClosePort()),
            ],
        }
    )
    collection = _FakeCollection()
    errors = _serve(instance, collection, ["a", "b"])

    assert errors == {}
    assert collection.closed
    assert [port for port, _ in collection.handled] == ["a", "b", "a"]


def test_serve_records_handler_exception_and_stops_that_port():
    instance = _instance(
        {
            "a": [Message(0.0, None, data="boom")],
            "b": [
                Message(0.0, None, data="d0"),
                Message(0.0, None, data=ClosePort()),
            ],
        }
    )
    collection = _FakeCollection(fail_on={"a"})
    errors = _serve(instance, collection, ["a", "b"])

    assert set(errors) == {"a"}
    assert isinstance(errors["a"], ValueError)
    # 'b' keeps going even though 'a' failed.
    assert [port for port, _ in collection.handled] == ["b"]
    assert collection.closed


def test_serve_ends_timeline_on_peer_crash():
    instance = Mock()
    instance._communicator.receive_message.side_effect = RuntimeError("gone")
    instance.should_save_snapshot.return_value = False
    collection = _FakeCollection()

    errors = _serve(instance, collection, ["a"])

    assert errors == {}
    assert collection.handled == []
    assert collection.closed


def test_serve_checkpoints_at_the_minimum_time_across_ports():
    instance = _instance(
        {
            "a": [Message(1.0, 2.0, data="d0"), Message(2.0, None, data=ClosePort())],
            "b": [Message(0.5, 1.5, data="d0"), Message(1.5, None, data=ClosePort())],
        },
        checkpoint_every=1,
    )
    collection = _FakeCollection()

    _serve(instance, collection, ["a", "b"])

    # Checkpointed with the slower port's time, not the faster one's.
    saved_times = [
        call.args[0].timestamp
        for call in instance.save_snapshot.call_args_list
    ]
    assert saved_times
    assert saved_times[0] == 0.5


# --- run_recorder_actor: settings wiring ------------------------------------


def _settings_instance(settings, s_ports, resuming=False):
    instance = Mock()
    instance.reuse_instance.side_effect = [True, False]
    instance.list_ports.return_value = {}
    instance.resuming.return_value = resuming

    def list_ports():
        from ymmsl.v0_2 import Operator

        return {Operator.S: s_ports}

    instance.list_ports.side_effect = list_ports
    instance.is_connected.return_value = True

    def get_setting(name, *args, **kwargs):
        if name in settings:
            return settings[name]
        if "default" in kwargs:
            return kwargs["default"]
        raise KeyError(name)

    instance.get_setting.side_effect = get_setting
    instance.should_save_final_snapshot.return_value = False
    return instance


def test_run_recorder_actor_warns_and_skips_when_no_ports(monkeypatch, tmp_path):
    instance = _settings_instance({}, s_ports=[])
    monkeypatch.setattr(
        "muscle3_dashboard.recorder.actor.Instance", lambda **kw: instance
    )
    called = []
    monkeypatch.setattr(
        "muscle3_dashboard.recorder.actor.RecorderCollection",
        lambda *a, **kw: called.append(True),
    )

    run_recorder_actor(deserializer_for_port=lambda p: (lambda data: data))

    assert called == []  # never got as far as building a collection


def test_run_recorder_actor_builds_collection_and_serves(monkeypatch, tmp_path):
    config = tmp_path / "cfg.py"
    config.write_text("def extract(payload):\n    return {}\n")
    store_path = tmp_path / "store"

    instance = _settings_instance(
        {"config": str(config), "store_path": str(store_path)},
        s_ports=["a_in"],
    )
    monkeypatch.setattr(
        "muscle3_dashboard.recorder.actor.Instance", lambda **kw: instance
    )
    monkeypatch.setattr(
        "muscle3_dashboard.recorder.actor._serve",
        lambda inst, collection, ports: {},
    )

    run_recorder_actor(deserializer_for_port=lambda p: (lambda data: data))

    assert store_path.is_dir()
    assert (store_path / config.name).is_file()  # config snapshot was made
    # libmuscle's checkpoint API guard requires should_init() right after
    # resuming(), regardless of whether its result is used.
    instance.should_init.assert_called_once()
    assert instance.mock_calls.index(("resuming", (), {})) < (
        instance.mock_calls.index(("should_init", (), {}))
    )


def test_run_recorder_actor_raises_and_reports_serve_errors(monkeypatch, tmp_path):
    config = tmp_path / "cfg.py"
    config.write_text("def extract(payload):\n    return {}\n")
    store_path = tmp_path / "store"

    instance = _settings_instance(
        {"config": str(config), "store_path": str(store_path)},
        s_ports=["a_in"],
    )
    monkeypatch.setattr(
        "muscle3_dashboard.recorder.actor.Instance", lambda **kw: instance
    )
    monkeypatch.setattr(
        "muscle3_dashboard.recorder.actor._serve",
        lambda inst, collection, ports: {"a_in": ValueError("boom")},
    )

    with pytest.raises(RuntimeError, match="a_in"):
        run_recorder_actor(deserializer_for_port=lambda p: (lambda data: data))

    instance.error_shutdown.assert_called_once()
