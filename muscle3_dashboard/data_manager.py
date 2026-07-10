import logging
import os
from pathlib import Path

import param

from muscle3_dashboard.loganalyzer.base import BaseLogAnalyzer
from muscle3_dashboard.loganalyzer.manager import ManagerLogAnalyzer

logger = logging.getLogger(__name__)

try:
    import ymmsl
    from ymmsl.v0_2 import Configuration
except ImportError:  # optional "graph" extra not installed
    ymmsl = None


def _instance_names(base: str, multiplicity: list[int]) -> list[str]:
    """Expand a component's multiplicity into the instance names the manager log
    uses (``worker`` with multiplicity ``[]`` -> ``["worker"]``; ``[2]`` ->
    ``["worker[0]", "worker[1]"]``; ``[2, 3]`` nests both dimensions)."""
    names = [base]
    for dim in multiplicity:
        names = [f"{name}[{i}]" for name in names for i in range(dim)]
    return names


class DataManager(param.Parameterized):
    data_updated = param.Event()

    def __init__(self, run_folder: Path):
        super().__init__()
        self.run_folder = run_folder
        self.logs_last_updated = None
        self.manager_log_analyzer: ManagerLogAnalyzer | None = None
        self.stdout_log_analyzers: dict[str, BaseLogAnalyzer] = {}
        self.stderr_log_analyzers: dict[str, BaseLogAnalyzer] = {}
        #: Instance name -> most recent mtime (epoch secs) across its
        #: stdout/stderr; drives the graph's activity glow without reading the
        #: log bodies (a cheap stat, so it stays correct if reads become lazy).
        self.instance_mtimes: dict[str, float] = {}
        self.update_run_folder(run_folder)

    def update_run_folder(self, run_folder: Path) -> None:
        """Set up log analyzers and simulation graph from run_folder"""
        self.run_folder = run_folder
        # TODO: setup notifications / poll until file exists?
        logfile = run_folder / "muscle3_manager.log"
        components = self._components_from_config(run_folder)
        self.manager_log_analyzer = ManagerLogAnalyzer(logfile, components)
        self.stdout_log_analyzers = {}
        self.stderr_log_analyzers = {}
        self._setup_component_logs(run_folder)
        self.manager_log_lines: list[str] = []
        self.stdout_log_lines: dict[str, list[str]] = {}
        self.stderr_log_lines: dict[str, list[str]] = {}

    def _components_from_config(self, run_folder: Path) -> list[str]:
        """Component instance names declared in the run's ``configuration.ymmsl``.

        Knowing them up front lets the graph and status views show every
        component (coloured "not started") before the manager log first mentions
        it. Returns an empty list when the optional ``ymmsl`` dependency or the
        config file is missing; the manager log then discovers components as it
        is parsed.
        """
        config = run_folder / "configuration.ymmsl"
        if ymmsl is None or not config.is_file():
            return []
        try:
            cfg = ymmsl.load_as(Configuration, config)
            names: list[str] = []
            for component in cfg.root_model().components.values():
                names.extend(
                    _instance_names(str(component.name), list(component.multiplicity))
                )
            return names
        except Exception as e:
            logger.warning("Could not read components from %s: %s", config, e)
            return []

    def _setup_component_logs(self, run_folder: Path) -> None:
        """Locate per-component logs under ``instances/<component>/``.

        ``muscle_manager --start-all`` launches every instance and redirects
        its stdout and stderr to ``instances/<component>/{stdout,stderr}.txt``.
        The dir is absent before a run starts (and for non-run folders), so its
        lookup must not be assumed.
        """
        instances = run_folder / "instances"
        if not instances.is_dir():
            return
        for component in instances.iterdir():
            # tail=True: only the last MAX_LINES are ever displayed, so don't
            # read the (potentially large) historical stdout/stderr at open.
            self.stdout_log_analyzers[component.name] = BaseLogAnalyzer(
                component / "stdout.txt", tail=True
            )
            self.stderr_log_analyzers[component.name] = BaseLogAnalyzer(
                component / "stderr.txt", tail=True
            )

    def update(self) -> None:
        """Update viewers whenever change in logfiles is detected"""
        self.update_manager_logfiles()
        self.update_stdout_logfiles()
        self.update_stderr_logfiles()
        self._refresh_instance_mtimes()
        self.data_updated = True

    def _refresh_instance_mtimes(self) -> None:
        """Record each instance's most recent stdout/stderr mtime."""
        mtimes: dict[str, float] = {}
        for analyzers in (self.stdout_log_analyzers, self.stderr_log_analyzers):
            for instance, analyzer in analyzers.items():
                try:
                    mtime = os.path.getmtime(analyzer.path)
                except OSError:
                    continue
                mtimes[instance] = max(mtimes.get(instance, 0.0), mtime)
        self.instance_mtimes = mtimes

    def update_logs_last_updated(self, log_analyzer: BaseLogAnalyzer) -> None:
        """Update logs_last_updated based on last time given file was
        modified"""
        file_time = log_analyzer.file_last_updated()
        self.logs_last_updated = max(self.logs_last_updated or file_time, file_time)

    def update_manager_logfiles(self) -> None:
        """Update manager logfile information in viewers"""
        self.manager_log_analyzer.update()
        self.manager_log_lines = self.manager_log_analyzer.pop_new_lines()
        self.update_logs_last_updated(self.manager_log_analyzer)

    def update_stdout_logfiles(self) -> None:
        """Update stdout logfiles information in viewers"""
        log_lines = {}
        for component, analyzer in self.stdout_log_analyzers.items():
            analyzer.update()
            log_lines[component] = self.stdout_log_analyzers[component].pop_new_lines()
            self.update_logs_last_updated(self.stdout_log_analyzers[component])

        self.stdout_log_lines = log_lines

    def update_stderr_logfiles(self) -> None:
        """Update stderr logfiles information in viewers"""
        log_lines = {}
        for component, analyzer in self.stderr_log_analyzers.items():
            analyzer.update()
            log_lines[component] = self.stderr_log_analyzers[component].pop_new_lines()
            self.update_logs_last_updated(self.stderr_log_analyzers[component])

        self.stderr_log_lines = log_lines
