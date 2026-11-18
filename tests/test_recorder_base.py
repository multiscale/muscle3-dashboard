from libmuscle import Message

from muscle3_dashboard.recorder.base import Recorder

# --- occurrence splitting (format-agnostic) --------------------------------


class _FakeRecorder(Recorder):
    """Records which occurrence bases were opened and which messages were
    written; writes nothing to disk."""

    def __init__(
        self, store_dir, deserialize, extract, profile, opened, record_interval=0.0
    ):
        super().__init__(store_dir, deserialize, extract, profile, record_interval)
        self._opened = opened
        self.written = []

    def _open_occurrence(self, base):
        self._opened.append(base.name)

    def _write(self, datasets):
        self.written.append(datasets["t"])
        return ""

    def _close_occurrence(self):
        pass


def _recorder(tmp_path, opened, record_interval=0.0):
    return _FakeRecorder(
        tmp_path,
        lambda data: data,
        lambda payload: {"t": payload},
        "cfg.py",
        opened,
        record_interval,
    )


def test_recorder_splits_occurrences_on_restart(tmp_path):
    """A backward time step / end-of-stream starts a new occurrence."""
    opened = []
    rec = _recorder(tmp_path, opened)
    # iteration 0: t=0,1 (1 ends the stream); iteration 1: t=0,1 (time resets).
    rec.handle(Message(0.0, 1.0, data=None))
    rec.handle(Message(1.0, None, data=None))
    rec.handle(Message(0.0, 1.0, data=None))
    rec.handle(Message(1.0, None, data=None))
    rec.close()
    assert opened == ["0000", "0001"]


def test_recorder_one_occurrence_for_monotonic(tmp_path):
    """A single monotonic trace stays one occurrence (no spurious split)."""
    opened = []
    rec = _recorder(tmp_path, opened)
    rec.handle(Message(0.0, 1.0, data=None))
    rec.handle(Message(1.0, 2.0, data=None))
    rec.handle(Message(2.0, None, data=None))
    rec.close()
    assert opened == ["0000"]


# --- checkpoint/resume bookkeeping ------------------------------------------


def test_recorder_state_roundtrip_resumes_same_occurrence(tmp_path):
    """A fresh Recorder restored from a mid-stream get_state() continues the
    same occurrence, rather than starting over at 0000."""
    opened = []
    rec = _recorder(tmp_path, opened)
    rec.handle(Message(0.0, 1.0, data=None))
    rec.handle(Message(1.0, None, data=None))  # ends occurrence 0000
    rec.handle(Message(0.0, 1.0, data=None))  # starts occurrence 0001

    state = rec.get_state()
    assert state == {
        "occurrence": 1,
        "last_time": 0.0,
        "prev_ended": False,
        "is_open": True,
        "last_recorded": 0.0,
    }

    # A new process would build a fresh Recorder and restore its state.
    resumed_opened = []
    resumed = _recorder(tmp_path, resumed_opened)
    resumed.restore_state(state)
    assert resumed_opened == ["0001"]  # reopened the still-open occurrence

    resumed.handle(Message(1.0, None, data=None))  # continues, doesn't split
    resumed.close()
    assert resumed_opened == ["0001"]


# --- record_interval ----------------------------------------------------------


def _stream(rec, times, data=None):
    """Feed ``times`` as one stream (the last message ends it); payload is t."""
    for i, t in enumerate(times):
        next_t = times[i + 1] if i + 1 < len(times) else None
        rec.handle(Message(t, next_t, data=t if data is None else data))


def test_record_interval_zero_records_everything(tmp_path):
    rec = _recorder(tmp_path, [])
    _stream(rec, [0.0, 0.1, 0.2, 0.3])
    assert rec.written == [0.0, 0.1, 0.2, 0.3]


def test_record_interval_thins_but_keeps_first_and_last(tmp_path):
    rec = _recorder(tmp_path, [], record_interval=0.5)
    times = [round(0.1 * i, 10) for i in range(13)]  # 0.0 .. 1.2
    _stream(rec, times)
    assert rec.written == [0.0, 0.5, 1.0, 1.2]


def test_record_interval_tolerates_float_accumulation(tmp_path):
    """0.1 summed five times is 0.49999999999999994, still counts as 0.5."""
    rec = _recorder(tmp_path, [], record_interval=0.5)
    times, t = [], 0.0
    for _ in range(7):
        times.append(t)
        t += 0.1
    _stream(rec, times)
    assert rec.written[:2] == [0.0, times[5]]


def test_record_interval_resets_per_occurrence(tmp_path):
    """A restarted stream records its first message again."""
    opened = []
    rec = _recorder(tmp_path, opened, record_interval=10.0)
    _stream(rec, [0.0, 1.0, 2.0])
    _stream(rec, [0.0, 1.0, 2.0])
    assert opened == ["0000", "0001"]
    assert rec.written == [0.0, 2.0, 0.0, 2.0]


def test_restore_state_without_last_recorded(tmp_path):
    """Snapshots taken before record_interval existed still resume."""
    rec = _recorder(tmp_path, [], record_interval=10.0)
    rec.restore_state(
        {"occurrence": 0, "last_time": 1.0, "prev_ended": False, "is_open": True}
    )
    rec.handle(Message(2.0, 3.0, data=2.0))
    rec.handle(Message(3.0, 4.0, data=3.0))
    assert rec.written == [2.0]
