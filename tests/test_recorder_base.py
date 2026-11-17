from libmuscle import Message

from muscle3_dashboard.recorder.base import Recorder

# --- occurrence splitting (format-agnostic) --------------------------------


class _FakeRecorder(Recorder):
    """Records which occurrence bases were opened; writes nothing to disk."""

    def __init__(self, store_dir, deserialize, extract, profile, opened):
        super().__init__(store_dir, deserialize, extract, profile)
        self._opened = opened

    def _open_occurrence(self, base):
        self._opened.append(base.name)

    def _write(self, datasets):
        return ""

    def _close_occurrence(self):
        pass


def _recorder(tmp_path, opened):
    return _FakeRecorder(
        tmp_path, lambda data: data, lambda payload: {}, "cfg.py", opened
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
    }

    # A new process would build a fresh Recorder and restore its state.
    resumed_opened = []
    resumed = _recorder(tmp_path, resumed_opened)
    resumed.restore_state(state)
    assert resumed_opened == ["0001"]  # reopened the still-open occurrence

    resumed.handle(Message(1.0, None, data=None))  # continues, doesn't split
    resumed.close()
    assert resumed_opened == ["0001"]
