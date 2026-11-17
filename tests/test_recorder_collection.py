from libmuscle import Message

from muscle3_dashboard.recorder.base import Recorder
from muscle3_dashboard.recorder.collection import (
    RecorderCollection,
    load_extract_config,
    snapshot_config,
)

# --- config loading ---------------------------------------------------------


def test_load_extract_fn(tmp_path):
    config = tmp_path / "config.py"
    config.write_text(
        "import xarray as xr\n"
        "def extract(payload):\n"
        "    return {'marker': xr.Dataset(\n"
        "        {'v': ('time', [payload['v']])},\n"
        "        coords={'time': [payload['t']]})}\n"
    )
    extract = load_extract_config(str(config))
    out = extract({"t": 0.0, "v": 1e6})
    assert set(out) == {"marker"}
    assert float(out["marker"]["v"].values[0]) == 1e6


def test_load_extract_state_class(tmp_path):
    config = tmp_path / "plot_file.py"
    config.write_text(
        "import xarray as xr\n"
        "from muscle3_dashboard.visualization.base_state import BaseState\n"
        "class State(BaseState):\n"
        "    def extract(self, payload):\n"
        "        self.data['source'] = xr.Dataset(\n"
        "            {'n': ('time', [float(payload['n'])])},\n"
        "            coords={'time': [payload['t']]})\n"
    )
    extract = load_extract_config(str(config))
    out = extract({"t": 0.0, "n": 3})
    assert set(out) == {"source"}
    assert float(out["source"]["n"].values[0]) == 3.0


def test_load_extract_config_rejects_other_files(tmp_path):
    config = tmp_path / "not_a_config.py"
    config.write_text("x = 1\n")
    try:
        load_extract_config(str(config))
        raise AssertionError("expected NameError")
    except NameError:
        pass


_BARE_STATE_CONFIG = (
    "from muscle3_dashboard.visualization.base_state import BaseState\n"
    "class State(BaseState):\n"
    "    pass\n"
)


def test_load_extract_state_without_extract_rejected_by_default(tmp_path):
    """A State that doesn't implement `extract` fails loudly unless
    `automatic_extract` is explicitly requested -- it might just be a typo,
    not an automatic-mode config."""
    config = tmp_path / "plot_file.py"
    config.write_text(_BARE_STATE_CONFIG)
    try:
        load_extract_config(str(config))
        raise AssertionError("expected NameError")
    except NameError:
        pass


_AUTOMATIC_STATE_CONFIG = (
    "import xarray as xr\n"
    "from muscle3_dashboard.visualization.base_state import BaseState\n"
    "class State(BaseState):\n"
    "    def automatic_extract(self, payload):\n"
    "        self.data['a/b'] = xr.Dataset(\n"
    "            {'a/b': ('time', [payload['v']])},\n"
    "            coords={'time': [payload['t']]})\n"
)


def test_load_extract_state_without_extract_falls_back_to_automatic(
    tmp_path,
):
    config = tmp_path / "plot_file.py"
    config.write_text(_AUTOMATIC_STATE_CONFIG)
    extract = load_extract_config(str(config), automatic_extract=True)
    out = extract({"t": 0.0, "v": 1e6})

    # automatic_extract's slashed full paths are flattened to dots (Zarr
    # rejects "/" in a variable name), both as dict keys and as each
    # dataset's own data variable name.
    assert "a.b" in out
    assert list(out["a.b"].data_vars) == ["a.b"]
    assert float(out["a.b"]["a.b"].values[0]) == 1e6


_TWO_FIELD_CONFIG = (
    "import xarray as xr\n"
    "def extract(payload):\n"
    "    t = payload['t']\n"
    "    return {\n"
    "        'a': xr.Dataset({'v': ('time', [1.0])}, coords={'time': [t]}),\n"
    "        'b': xr.Dataset({'v': ('time', [2.0])}, coords={'time': [t]}),\n"
    "    }\n"
)


def test_load_extract_config_no_fields_keeps_everything(tmp_path):
    config = tmp_path / "config.py"
    config.write_text(_TWO_FIELD_CONFIG)
    extract = load_extract_config(str(config))
    assert set(extract({"t": 0.0})) == {"a", "b"}


def test_load_extract_config_fields_restricts_output(tmp_path):
    config = tmp_path / "config.py"
    config.write_text(_TWO_FIELD_CONFIG)
    extract = load_extract_config(str(config), fields=["a"])
    assert set(extract({"t": 0.0})) == {"a"}


def test_snapshot_config_copies_next_to_data(tmp_path):
    config = tmp_path / "cfg.py"
    config.write_text("x = 1\n")
    store_path = tmp_path / "store"
    store_path.mkdir()

    snapshot = snapshot_config(config, store_path)
    assert snapshot == store_path / "cfg.py"
    # Editing the original leaves the snapshot untouched...
    config.write_text("x = 2\n")
    assert snapshot.read_text() == "x = 1\n"
    # ...until the next run re-snapshots it.
    assert snapshot_config(config, store_path).read_text() == "x = 2\n"
    # Config already next to the data: returned as-is, no self-copy.
    assert snapshot_config(snapshot, store_path) == snapshot


# --- collection wiring -------------------------------------------------------

_CONFIG = """
import xarray as xr


def extract(payload):
    t = payload["t"]
    return {
        payload["name"]: xr.Dataset(
            {"t": ("time", [t])}, coords={"time": [t]}
        )
    }
"""


class _RecordingRecorder(Recorder):
    """A no-op Recorder that just remembers what it was asked to write."""

    def __init__(self, store_dir, deserialize, extract, profile, log):
        super().__init__(store_dir, deserialize, extract, profile)
        self._log = log

    def _open_occurrence(self, base):
        pass

    def _write(self, datasets):
        self._log.append(datasets)
        return ""

    def _close_occurrence(self):
        pass


def test_collection_routes_per_port(tmp_path):
    config = tmp_path / "config.py"
    config.write_text(_CONFIG)
    store_path = tmp_path / "store"
    store_path.mkdir()
    log = []

    def make_recorder(store_dir, deserialize, extract, profile):
        return _RecordingRecorder(store_dir, deserialize, extract, profile, log)

    collection = RecorderCollection(
        store_path, config, {"equilibrium_in": lambda data: data}, make_recorder
    )
    assert collection.config_snapshot == store_path / "config.py"


def test_collection_fields_restrict_what_gets_recorded(tmp_path):
    config = tmp_path / "config.py"
    config.write_text(_TWO_FIELD_CONFIG)
    store_path = tmp_path / "store"
    store_path.mkdir()
    log = []

    def make_recorder(store_dir, deserialize, extract, profile):
        return _RecordingRecorder(store_dir, deserialize, extract, profile, log)

    collection = RecorderCollection(
        store_path,
        config,
        {"equilibrium_in": lambda data: data},
        make_recorder,
        fields=["a"],
    )
    collection.handle("equilibrium_in", Message(0.0, None, data={"t": 0.0}))

    assert len(log) == 1
    assert set(log[0]) == {"a"}
