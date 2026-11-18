import pytest
from libmuscle import Message
from ymmsl.v0_2 import Operator

from muscle3_dashboard.recorder.actor import run_recorder_actor

_ACTOR = "muscle3_dashboard.recorder.actor"


class _FakeCollection:
    """Stands in for RecorderCollection."""

    def __init__(self, fail_on=None):
        self.handled: list[tuple[str, float]] = []
        self.restored = None
        self.closed = False
        self._fail_on = fail_on or set()

    def handle(self, port, msg):
        if port in self._fail_on:
            raise ValueError(f"boom on {port}")
        self.handled.append((port, msg.timestamp))
        return "ok"

    def get_state(self):
        return {"handled": len(self.handled)}

    def restore_state(self, state):
        self.restored = state

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def _in_tmp_path(monkeypatch, tmp_path):
    """The default store_path is the cwd; keep test output out of the repo."""
    monkeypatch.chdir(tmp_path)


def _instance(iterations, ports=None, settings=None, resuming=False):
    """A Mock Instance running one reuse iteration per entry of
    ``iterations`` (each a ``{port: timestamp}`` dict), connected on
    ``ports`` (operator -> names; defaults to F_INIT on the iterations'
    ports)."""
    instance = Mock()
    instance.reuse_instance.side_effect = [True] * len(iterations) + [False]
    if ports is None:
        ports = {Operator.F_INIT: sorted(iterations[0]) if iterations else []}
    instance.list_ports.return_value = ports
    instance.is_connected.return_value = True
    instance.resuming.side_effect = [resuming] + [False] * len(iterations)
    instance.load_snapshot.return_value = Message(0.0, data={"saved": True})
    instance.should_init.return_value = True
    instance.should_save_final_snapshot.return_value = False

    queues = {}
    for it in iterations:
        for port, t in it.items():
            queues.setdefault(port, []).append(Message(t, data=f"{port}@{t}"))
    instance.receive.side_effect = lambda port: queues[port].pop(0)

    settings = settings or {"config": "cfg.py"}

    def get_setting(name, *args, **kwargs):
        if name in settings:
            return settings[name]
        if "default" in kwargs:
            return kwargs["default"]
        raise KeyError(name)

    instance.get_setting.side_effect = get_setting
    return instance


def _run(monkeypatch, instance, collection=None):
    monkeypatch.setattr(f"{_ACTOR}.Instance", lambda **kw: instance)
    if collection is not None:
        monkeypatch.setattr(f"{_ACTOR}.RecorderCollection", lambda *a, **kw: collection)
    run_recorder_actor(deserializer_for_port=lambda p: lambda data: data)


def test_records_one_message_per_port_per_iteration(monkeypatch):
    collection = _FakeCollection()
    instance = _instance([{"a": 0.0, "b": 0.0}, {"a": 1.0, "b": 1.0}])

    _run(monkeypatch, instance, collection)

    assert collection.handled == [("a", 0.0), ("b", 0.0), ("a", 1.0), ("b", 1.0)]
    assert collection.closed


def test_warns_and_skips_when_no_ports(monkeypatch):
    built = []
    instance = _instance([{}], ports={})
    monkeypatch.setattr(f"{_ACTOR}.Instance", lambda **kw: instance)
    monkeypatch.setattr(
        f"{_ACTOR}.RecorderCollection", lambda *a, **kw: built.append(True)
    )

    run_recorder_actor(deserializer_for_port=lambda p: lambda data: data)

    assert built == []


def test_rejects_s_ports(monkeypatch):
    instance = _instance([{"a": 0.0}], ports={Operator.S: ["a"]})

    with pytest.raises(RuntimeError, match="must be f_init"):
        _run(monkeypatch, instance, _FakeCollection())

    instance.error_shutdown.assert_called_once()


def test_handler_error_shuts_down_and_closes(monkeypatch):
    collection = _FakeCollection(fail_on={"a"})
    instance = _instance([{"a": 0.0}])

    with pytest.raises(ValueError, match="boom"):
        _run(monkeypatch, instance, collection)

    instance.error_shutdown.assert_called_once()
    assert collection.closed


def test_saves_final_snapshot_at_latest_time(monkeypatch):
    collection = _FakeCollection()
    instance = _instance([{"a": 0.5, "b": 1.5}])
    instance.should_save_final_snapshot.return_value = True

    _run(monkeypatch, instance, collection)

    (call,) = instance.save_final_snapshot.call_args_list
    assert call.args[0].timestamp == 1.5
    assert call.args[0].data == {"handled": 2}


def test_resume_restores_snapshot(monkeypatch):
    collection = _FakeCollection()
    instance = _instance([{"a": 2.0}], resuming=True)

    _run(monkeypatch, instance, collection)

    assert collection.restored == {"saved": True}
    assert collection.handled == [("a", 2.0)]


def test_resuming_called_before_should_init(monkeypatch):
    """libmuscle's checkpoint API requires resuming() then should_init()
    in every reuse iteration."""
    instance = _instance([{"a": 0.0}, {"a": 1.0}])

    _run(monkeypatch, instance, _FakeCollection())

    names = [c[0] for c in instance.mock_calls if c[0] in ("resuming", "should_init")]
    assert names == ["resuming", "should_init"] * 2


def test_builds_real_collection_from_settings(monkeypatch, tmp_path):
    config = tmp_path / "cfg.py"
    config.write_text("def extract(payload):\n    return {}\n")
    store_path = tmp_path / "store"
    instance = _instance(
        [{"a": 0.0}],
        settings={"config": str(config), "store_path": str(store_path)},
    )

    _run(monkeypatch, instance)

    assert (store_path / config.name).is_file()  # config snapshot was made


def test_refuses_non_empty_port_dir(monkeypatch, tmp_path):
    config = tmp_path / "cfg.py"
    config.write_text("def extract(payload):\n    return {}\n")
    keep = tmp_path / "a" / "precious.txt"
    keep.parent.mkdir()
    keep.write_text("x")
    instance = _instance(
        [{"a": 0.0}],
        settings={"config": str(config), "store_path": str(tmp_path)},
    )

    with pytest.raises(RuntimeError, match="is not empty"):
        _run(monkeypatch, instance)

    assert keep.read_text() == "x"
    instance.error_shutdown.assert_called_once()
