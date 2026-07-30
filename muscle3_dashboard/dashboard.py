import html
from datetime import datetime
from functools import lru_cache
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import panel as pn

from muscle3_dashboard.components.component_summary import ComponentSummaryViewer
from muscle3_dashboard.components.crash_analysis import CrashAnalysisViewer
from muscle3_dashboard.components.log_files import LogFilesViewer
from muscle3_dashboard.components.profiling_information import (
    ProfilingInformationViewer,
)
from muscle3_dashboard.components.ymmsl_graph import YmmslGraphViewer
from muscle3_dashboard.data_manager import DataManager
from muscle3_dashboard.instances import base_name
from muscle3_dashboard.panel_util import add_session_periodic_callback
from muscle3_dashboard.pathlink import path_html

# Material design gives cleaner cards/typography than the default.
pn.extension("tabulator", design="material", sizing_mode="stretch_width")

#: Header status-dot colours, light Material shades for the dark header.
_STATE_COLORS = {
    "not started": "#ffb74d",
    "running": "#81c784",
    "finished": "#bdbdbd",
    "failed": "#e57373",
}


@lru_cache(maxsize=1)
def _dashboard_version() -> str:
    """Version for the title.

    When installed, read the distribution metadata. When run from a
    source checkout (no installed distribution), derive a version from
    git via setuptools_scm, which this project already uses. Fall back
    to 'dev' if neither is available (e.g. an unpacked tarball).
    """
    try:
        return version("muscle3-dashboard")
    except PackageNotFoundError:
        pass
    try:
        from setuptools_scm import get_version

        return get_version(root="..", relative_to=__file__)
    except Exception:
        return "dev"


class Dashboard(pn.viewable.Viewer):
    """Main dashboard for muscle3_dashboard app."""

    def __init__(self, run_folder: Path) -> None:
        self.run_folder = run_folder

        self.template = pn.template.MaterialTemplate(
            collapsed_sidebar=True,
            title=f"MUSCLE3 Dashboard | {_dashboard_version()}",
        )

        self.data_manager = DataManager(run_folder)

        self.ymmsl_graph_viewer = YmmslGraphViewer(
            self.data_manager,
            on_select=self._show_logs_for,
        )
        self.crash_analysis_viewer = CrashAnalysisViewer(self.data_manager)
        self.component_summary_viewer = ComponentSummaryViewer(self.data_manager)
        self.log_files_viewer = LogFilesViewer(self.data_manager)
        self.profiling_information_viewer = ProfilingInformationViewer(
            self.data_manager
        )

        # Header strip: status dot + state, the run name, the copyable run
        # directory, and (right-aligned) the time of the last log update.
        self.header_pane = pn.pane.HTML(
            self._header_html(), sizing_mode="stretch_width"
        )
        self.template.header.append(self.header_pane)
        self.data_manager.param.watch(self._update_header, "data_updated")
        # Auto-open the responsible component's log on the first detected crash.
        # Registered after the viewers so their log state is populated first.
        self._auto_opened = False
        self.data_manager.param.watch(self._auto_open_crash, "data_updated")

        # Single page, top to bottom: a crash banner (only on failure), the
        # simulation graph (components coloured by status, click one for its
        # summary + logs), the clicked component's summary, then the log files.
        self.template.main.append(
            pn.Column(
                self.crash_analysis_viewer,
                self.ymmsl_graph_viewer,
                self.component_summary_viewer,
                self.log_files_viewer,
                sizing_mode="stretch_width",
            )
        )

        # Populate everything once now (reads the logs, colours the graph, and
        # auto-opens the responsible component's log for an already-crashed run)
        # so the page is correct at load instead of after the first poll.
        self.data_manager.update()

        self.session_created()

    def _header_html(self) -> str:
        analyzer = self.data_manager.manager_log_analyzer
        state = analyzer.simulation_state
        tip = html.escape(analyzer.status_message or state)
        return (
            '<div style="display:flex;align-items:baseline;gap:0.6em;width:100%">'
            f'<span title="{tip}" style="white-space:nowrap">'
            f'<span style="color:{_STATE_COLORS[state]};font-size:1.1em">'
            f"&#x25cf;</span> {state}</span>"
            f"<b>{html.escape(self.run_folder.name)}</b>"
            f"<span>{path_html(self.run_folder)}</span>"
            f'<span style="margin-left:auto;opacity:0.7;font-size:0.85em;'
            f'white-space:nowrap">{self._updated_html()}</span>'
            "</div>"
        )

    def _updated_html(self) -> str:
        """Absolute time of the most recent write across all log files."""
        ts = self.data_manager.logs_last_updated
        if ts is None:
            return ""
        age = (datetime.now() - ts).total_seconds()
        if age < 60:
            rel = f"{max(0, int(age))} s ago"
        elif age < 3600:
            rel = f"{int(age // 60)} min ago"
        else:
            rel = f"{int(age // 3600)} h ago"
        return f"last log write {ts.strftime('%H:%M:%S')} ({rel})"

    def _update_header(self, event) -> None:
        self.header_pane.object = self._header_html()

    def _responsible_component(self) -> str | None:
        """Base name of the likely-responsible crashed component, if any.

        The culprit is one that exited with a real non-zero code, not a
        collateral SIGKILL (-9) / generic crash after another failed. Uses the
        same structured classifier (``Component.crash_kind``) as the graph, so
        the auto-opened log and the graph's red outline always agree. Returns
        the first culprit's base name.
        """
        components = self.data_manager.manager_log_analyzer.components
        for name, component in components.items():
            if component.crash_kind == "culprit":
                return base_name(name)
        return None

    def _auto_open_crash(self, event) -> None:
        """On the first detected crash, show the responsible component's log."""
        if self._auto_opened:
            return
        responsible = self._responsible_component()
        if responsible is None:
            return
        self._auto_opened = True
        # show_source picks stderr when it has output, which is where the
        # crashing component's traceback lives.
        self._show_logs_for(responsible)

    def _show_logs_for(self, source: str) -> None:
        """Show the clicked component's summary and its log."""
        self.component_summary_viewer.show(source)
        self.log_files_viewer.show_source(source)

    def session_created(self) -> None:
        """Poll the logs once the session has loaded."""
        # TODO: use watchfiles to subscribe to notifications instead of polling?
        add_session_periodic_callback(self.data_manager.update, 1000)

    def __panel__(self):
        return self.template
