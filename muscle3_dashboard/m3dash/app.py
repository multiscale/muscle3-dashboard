"""m3dash Panel application.

Serves two Panel apps:

* ``/``: a landing page listing the user's MUSCLE3 runs, discovered via
  SLURM, local muscle_manager processes and a filesystem scan of
  configurable run roots (see :mod:`muscle3_dashboard.m3dash.discovery`).
* ``/run?dir=<path>``: the muscle3-dashboard for one run directory.

The server listens on a per-user unix socket (``~/.m3dash.sock``, mode
0600, which on a shared login node restricts access to the owning user)
and is reached through an SSH forward, e.g. in ``~/.ssh/config``::

    LocalForward 127.0.0.1:4333 /home/ITER/%r/.m3dash.sock

(``%r`` expands to the remote username, so one line serves every user.)
"""

import html
import logging
import os
import socket
import threading
import time
import urllib.parse
import webbrowser
from datetime import datetime
from pathlib import Path

import panel as pn
from tornado.netutil import bind_unix_socket

from muscle3_dashboard.m3dash.discovery import (
    MANAGER_LOG,
    Run,
    RunStatus,
    discover_runs,
)
from muscle3_dashboard.panel_util import add_session_periodic_callback

logger = logging.getLogger(__name__)

# Incremental discovery (cached dir listings + log status, parallel stat) makes
# rescans cheap after the first, so they can run often.
RESCAN_INTERVAL_SECONDS = 5
VIEW_REFRESH_MILLISECONDS = 5000

#: Sentinel for runs with no known update time, so they sort oldest.
_EPOCH = datetime.fromtimestamp(0)


class RunIndex:
    """Shared, periodically refreshed cache of discovered runs."""

    def __init__(self, roots: list[Path]) -> None:
        self.roots = roots
        self.runs: list[Run] = []
        self.last_scan: datetime | None = None
        self.scan_seconds: float | None = None
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._scan_loop, name="m3dash-scanner", daemon=True
        )
        self._thread.start()

    def _scan_loop(self) -> None:
        while True:
            started = time.monotonic()
            try:
                runs = discover_runs(self.roots)
                with self._lock:
                    self.runs = runs
            except Exception:
                logger.exception("Run discovery failed")
            finally:
                self.scan_seconds = time.monotonic() - started
                self.last_scan = datetime.now()
            time.sleep(RESCAN_INTERVAL_SECONDS)


_index: RunIndex | None = None


def _prewarm() -> None:
    """Import the run-page modules ahead of the first request.

    ``run_app`` imports the dashboard lazily so the landing page stays light,
    but that import (Panel + ymmsl2svg + pandas + bokeh) takes ~1.5 s, paid by
    whoever first opens a run. Doing it in a background thread at startup means
    the import is already cached (or in progress, behind the import lock) by the
    time a run is opened, without delaying the server or the landing page.
    """
    try:
        import muscle3_dashboard.dashboard  # noqa: F401
    except Exception:
        logger.exception("Dashboard pre-warm import failed")


def _age(timestamp: datetime | None) -> str:
    if timestamp is None:
        return "?"
    seconds = (datetime.now() - timestamp).total_seconds()
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds / 60)}m"
    if seconds < 86400:
        return f"{int(seconds / 3600)}h"
    return f"{int(seconds / 86400)}d"


# Status dot colours: blue = running, green = finished OK, red = failed,
# grey = not started / unknown.
_DOT_COLORS = {
    RunStatus.NOT_STARTED: "#bdbdbd",
    RunStatus.RUNNING: "#1976d2",
    RunStatus.FINISHED: "#2e7d32",
    RunStatus.FAILED: "#d32f2f",
    RunStatus.UNKNOWN: "#9e9e9e",
}


def _job_ref(run: Run) -> str:
    if run.job_id:
        name = run.job_name
        return f"{name} (job {run.job_id})" if name else f"job {run.job_id}"
    if run.pid:
        return f"pid {run.pid}"
    return ""


def _common_prefix_len(a: str, b: str) -> int:
    """Length of the longest path prefix a and b share up to a '/' boundary."""
    cut = 0
    for i in range(min(len(a), len(b))):
        if a[i] != b[i]:
            break
        if a[i] == "/":
            cut = i + 1
    return cut


def _common_dir(paths: list[str]) -> str:
    """Directory prefix shared by every path (no trailing slash)."""
    if not paths:
        return ""
    common = os.path.commonpath(paths)
    # If the common part is itself a whole run path (e.g. a single run), back
    # off to its parent so the run still shows a name.
    if common in paths:
        common = os.path.dirname(common)
    return common


def _updated_html(timestamp: datetime | None) -> str:
    if timestamp is None:
        return "?"
    return (
        f"{timestamp.strftime('%Y-%m-%d %H:%M:%S')} "
        f'<span style="opacity:0.55">({_age(timestamp)})</span>'
    )


def _runs_table_html(runs: list[Run]) -> str:
    """Render the run list as an HTML table keyed on the run directory.

    Runs are sorted newest-first; the directory prefix common to all runs is
    dropped (shown once above the table) so each row's clickable path shows only
    its distinguishing tail and opens /run. A status dot precedes the path.
    Per-component web UIs live on the run page, not here.
    """
    if not runs:
        return (
            '<table style="border-spacing:12px 4px">'
            "<tr><td><i>No runs found yet.</i></td></tr></table>"
        )
    common = _common_dir([str(run.run_dir) for run in runs])
    rows = []
    previous = ""
    for run in sorted(runs, key=lambda r: r.last_updated or _EPOCH, reverse=True):
        path = str(run.run_dir)
        rel = path[len(common) :].lstrip("/") if common else path
        # Blank out the path components shared with the row above (replacing
        # them with spaces) so only the distinguishing tail shows, aligned.
        cut = _common_prefix_len(previous, rel)
        previous = rel
        indent, unique = " " * cut, html.escape(rel[cut:])
        dot = _DOT_COLORS[run.status]
        # A not-yet-started (queued) run has no manager log to open yet, so show
        # its path as plain text rather than a /run link.
        if run.status is RunStatus.NOT_STARTED:
            label = unique
        else:
            query = urllib.parse.urlencode({"dir": path})
            label = f'<a href="run?{query}" target="_blank">{unique}</a>'
        rows.append(
            "<tr>"
            '<td style="font-family:monospace;white-space:pre">'
            f'<span style="color:{dot}" title="{html.escape(run.status.value)}">'
            "●</span> "
            f"{indent}{label}"
            "</td>"
            f'<td style="white-space:nowrap">{_updated_html(run.last_updated)}</td>'
            f"<td>{html.escape(_job_ref(run))}</td>"
            "</tr>"
        )
    caption = (
        f'<div style="opacity:0.7;margin-bottom:6px">in '
        f"<code>{html.escape(common)}/</code></div>"
        if common
        else ""
    )
    return (
        caption + '<table style="border-spacing:12px 4px">'
        "<tr><th>Run directory</th><th>Updated</th><th>Job/PID</th></tr>"
        + "".join(rows)
        + "</table>"
    )


def _scan_summary_html(index: RunIndex) -> str:
    if index.last_scan is not None:
        when = f"last scan {_age(index.last_scan)} ago ({index.scan_seconds:.2f}s)"
    else:
        when = "first scan pending"
    roots = ", ".join(f"<code>{html.escape(str(r))}</code>" for r in index.roots)
    tip = html.escape("run roots given on the command line; restart to change")
    return f'<span style="opacity:0.7" title="{tip}">{when} · roots: {roots}</span>'


def index_app():
    """Landing page: run list, with the scan summary + roots at the bottom.

    The view re-renders every ``VIEW_REFRESH_MILLISECONDS`` from the
    snapshot kept up to date by the background scanner.
    """
    assert _index is not None
    table = pn.pane.HTML(_runs_table_html(_index.runs), sizing_mode="stretch_width")
    summary = pn.pane.HTML(sizing_mode="stretch_width")

    def refresh(*_events) -> None:
        table.object = _runs_table_html(_index.runs)
        summary.object = _scan_summary_html(_index)

    add_session_periodic_callback(refresh, VIEW_REFRESH_MILLISECONDS)
    refresh()

    return pn.template.VanillaTemplate(
        title="m3dash | MUSCLE3 runs",
        main=[pn.Column(table, summary)],
    )


def run_app():
    """Dashboard page for a single run, selected with ?dir=<path>."""
    raw = pn.state.session_args.get("dir", [b""])[0].decode()
    run_dir = Path(raw).expanduser()
    if not raw or not (run_dir / MANAGER_LOG).is_file():
        # NB: a bare pane returned from an app function may render an
        # empty document; wrap in a template like the other pages.
        return pn.template.VanillaTemplate(
            title="m3dash | error",
            main=[
                pn.pane.Markdown(
                    f"Not a MUSCLE3 run directory (no `{MANAGER_LOG}`): "
                    f"`{raw or '(missing ?dir= parameter)'}`"
                )
            ],
        )
    # Local import: pulls in the full dashboard and its dependencies
    from muscle3_dashboard.dashboard import Dashboard

    return Dashboard(run_dir.resolve())


def _socket_alive(socket_path: Path) -> bool:
    """True if something is already accepting connections on the socket."""
    if not socket_path.exists():
        return False
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
        try:
            probe.connect(str(socket_path))
            return True
        except OSError:
            return False


def _claim_socket(socket_path: Path) -> None:
    """Remove a stale socket, or fail if a live server owns it."""
    if _socket_alive(socket_path):
        raise RuntimeError(f"m3dash is already serving on {socket_path}")
    socket_path.unlink(missing_ok=True)


def _origins(port: int) -> list[str]:
    """Allowed websocket origins for a port reached over loopback."""
    return [f"localhost:{port}", f"127.0.0.1:{port}"]


def serve(
    socket_path: Path | None,
    roots: list[Path],
    local_port: int | None = None,
    tcp_port: int | None = None,
    open_browser: bool = False,
) -> None:
    """Run the m3dash server (blocking).

    Args:
        socket_path: Unix socket to serve on (mode 0600), or None.
        roots: Run roots scanned for runs (fixed for the server's life).
        local_port: Port the SSH forward uses; allows its websocket origin
            and is logged as the URL the socket is reachable at. Socket-only.
        tcp_port: Optional loopback TCP port to also serve on (its
            origin is allowed automatically).
        open_browser: Open a browser at the TCP URL once serving (for a
            desktop session running on the node itself). Needs tcp_port.
    """
    global _index
    _index = RunIndex(roots)
    _index.start()
    logger.info(
        "Scanning for runs under: %s",
        ", ".join(str(root) for root in roots) or "(none)",
    )
    threading.Thread(target=_prewarm, name="m3dash-prewarm", daemon=True).start()

    origins = (_origins(local_port) if socket_path and local_port else []) + (
        _origins(tcp_port) if tcp_port else []
    )
    server = pn.serve(
        {"/": index_app, "/run": run_app},
        address="127.0.0.1",
        port=tcp_port or 0,
        websocket_origin=origins,
        show=False,
        start=False,
        threaded=False,
    )
    if socket_path is not None:
        _claim_socket(socket_path)
        socket_path.parent.mkdir(parents=True, exist_ok=True)
        server._http.add_sockets([bind_unix_socket(str(socket_path), mode=0o600)])
        logger.info("Serving on unix socket %s", socket_path)
        if local_port:
            logger.info(
                "→ reachable at http://localhost:%d/ (via your SSH LocalForward)",
                local_port,
            )
    if tcp_port:
        logger.info("Serving at http://localhost:%d/", tcp_port)
    if open_browser and tcp_port:
        url = f"http://localhost:{tcp_port}/"
        server.io_loop.add_callback(lambda: webbrowser.open(url))
    try:
        server.start()
        server.io_loop.start()
    finally:
        if socket_path is not None:
            socket_path.unlink(missing_ok=True)
