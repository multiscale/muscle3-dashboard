from pathlib import Path

from muscle3_dashboard.components.recorder_viewer import _resolve_plot_file


def _port_dirs(tmp_path: Path) -> list[Path]:
    port = tmp_path / "workdir" / "equilibrium_in"
    port.mkdir(parents=True)
    return [port]


def test_resolve_plot_file_prefers_workdir_snapshot(tmp_path):
    port_dirs = _port_dirs(tmp_path)
    original = tmp_path / "elsewhere" / "plot.py"
    snapshot = port_dirs[0].parent / "plot.py"
    snapshot.write_text("# recorded snapshot\n")

    assert _resolve_plot_file(port_dirs, str(original)) == str(snapshot)


def test_resolve_plot_file_falls_back_to_original(tmp_path):
    port_dirs = _port_dirs(tmp_path)
    original = tmp_path / "elsewhere" / "plot.py"

    assert _resolve_plot_file(port_dirs, str(original)) == str(original)


def test_resolve_plot_file_none(tmp_path):
    assert _resolve_plot_file(_port_dirs(tmp_path), None) is None