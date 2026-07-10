import re
import signal
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path

import pandas as pd
import param
from bokeh.core.serialization import Serializable, Serializer

from muscle3_dashboard.constants import MAX_LINES
from muscle3_dashboard.loganalyzer.base import BaseLogAnalyzer

_LOGPARSER = re.compile(
    r"""
    ^(?P<component>\S+)         # Source of log message: muscle_manager or remote component
    \ +(?P<datetime>\S+\ \S+)   # Date + time; the source column is padded, allow several spaces
    \ +(?P<loglevel>\S+)        # Log level: INFO / DEBUG / etc.
    \ +(?P<name>\S+):           # Python module for manager logs, or remote component name
    \s*(?P<message>.*)$         # Log message
    """,  # noqa: E501
    re.VERBOSE,
)


class ComponentStatus(Serializable, Enum):
    """Defines the status in the simulation for given component"""

    NOT_STARTED = "Not started"
    PLANNED = "Planned"
    INSTANTIATING = "Instantiating"
    REGISTERED = "Registered"
    DEREGISTERED = "Deregistered"
    FINISHED = "Finished"

    def to_serializable(self, serializer: Serializer) -> str:
        """Converts this object to a serializable representation."""
        return self.value


@dataclass
class Component:
    """Class to keep track of exit_code and status per muscle3 component"""

    name: str
    exit_code: str = ""
    status: ComponentStatus = ComponentStatus.NOT_STARTED

    @property
    def exit_code_message(self):
        """Show standard meaning for given exit code if available"""
        try:
            if int(self.exit_code) < 0:
                exit_code_str = signal.strsignal(-1 * int(self.exit_code))
                return f"{str(self.exit_code)}: {exit_code_str}"
        except ValueError:
            pass  # exit_code could not be parsed as integer
        return self.exit_code

    @property
    def crash_kind(self) -> str | None:
        """Classify this component's exit for crash triage, or None if clean.

        Reads the structured ``exit_code`` (not the formatted message) so the
        classification is exact:

        * ``"culprit"`` -- a real non-zero exit code (or a crash signal other
          than SIGKILL): the likely root cause of a failure.
        * ``"killed"`` -- SIGKILL (``-9``) or a generic ``"crashed"`` with no
          code: usually collateral damage after another component failed first.
        * ``None`` -- no exit recorded, or a clean exit (``""``/``"0"``).
        """
        code = self.exit_code
        if code in ("", "0"):
            return None
        if code == "crashed":
            return "killed"
        try:
            return "killed" if int(code) == -9 else "culprit"
        except ValueError:
            # An unrecognised, non-empty code: treat as a real failure.
            return "culprit"


class ManagerLogAnalyzer(BaseLogAnalyzer):
    """Log analyzer for muscle_manager log file"""

    muscle_manager_version = param.String(default="unknown")
    """Version string of the muscle manager"""
    start_time = param.Date()
    """Time of the first log message"""
    last_update_time = param.Date()
    """Time of the most recent log message"""
    status = param.String()
    """Current status of the muscle manager"""

    lines_read = param.Integer(0)
    """Number of log lines read"""
    lines_parsed = param.Integer(0)
    """Number of log lines successfully parsed"""

    messages_per_level = param.Dict()
    """Number of parsed messages per log level"""
    new_lines = param.List()
    """New lines to be added"""

    def __init__(self, logfile: Path, components: list[str]) -> None:
        self.components: dict[str, Component] = {
            component: Component(
                component, status=ComponentStatus.NOT_STARTED, exit_code=""
            )
            for component in components
        }
        self._messages_per_level: dict[str, int] = {
            "DEBUG": 0,
            "INFO": 0,
            "WARNING": 0,
            "ERROR": 0,
            "CRITICAL": 0,
            "unknown": 0,
        }
        self.messages_per_level_by_source: dict[str, dict[str, int]] = {}
        """Number of parsed messages per log level, per source (the
        muscle_manager itself or a remote component)"""
        self._lines_read = 0
        self._lines_parsed = 0
        super().__init__(logfile)

    def update(self) -> None:
        """Parse new lines of log file and update parsed information"""
        # Parse currently available log lines
        log_lines = []
        for line in self._file:
            log_lines.append(line)
            self._lines_read += 1
            match = _LOGPARSER.match(line)
            if match is None:
                continue  # FIXME: notify user about this?
            try:
                timestamp = datetime.fromisoformat(match.group("datetime"))
            except ValueError:
                # A non-log line that happens to fit the column layout, e.g. a
                # Python traceback ("Traceback (most recent call last):"); skip
                # it rather than crash the whole scan on fromisoformat.
                continue
            self._lines_parsed += 1
            if self.start_time is None:
                self.start_time = timestamp
            self.last_update_time = timestamp

            if match.group("component") == "muscle_manager":
                # TODO: suppress any exceptions when parsing the message?
                self._parse_manager_log_message(match.group("message"))

            loglevel = match.group("loglevel")
            if loglevel not in self._messages_per_level:
                loglevel = "unknown"
            self._messages_per_level[loglevel] += 1
            per_source = self.messages_per_level_by_source.setdefault(
                match.group("component"), dict.fromkeys(self._messages_per_level, 0)
            )
            per_source[loglevel] += 1

        # Update externally visible state. The full file is still parsed above
        # (statuses/exit codes are scattered throughout); new_lines feeds only
        # the terminal, which shows the last MAX_LINES, so cap it there.
        combined = self.new_lines + log_lines
        if len(combined) > MAX_LINES:
            combined = combined[-MAX_LINES:]
        self.param.update(
            lines_read=self._lines_read,
            lines_parsed=self._lines_parsed,
            messages_per_level=self._messages_per_level.copy(),
            new_lines=combined,
        )

    def _parse_manager_log_message(self, message: str) -> None:
        """Obtain status and exit code for given component by parsing log
        message"""
        if message.startswith("Libmuscle version"):
            self.muscle_manager_version = message.split()[-1]
        elif message.startswith("Planned"):
            _, component, _ = message.split(maxsplit=2)
            self._comp(component).status = ComponentStatus.PLANNED
        elif message.startswith("Instantiating"):
            _, component = message.split(maxsplit=1)
            self._comp(component).status = ComponentStatus.INSTANTIATING
        elif message.startswith("Registered"):
            _, _, component = message.split(maxsplit=2)
            self._comp(component).status = ComponentStatus.REGISTERED
        elif message.startswith("Deregistered"):
            _, _, component = message.split(maxsplit=2)
            self._comp(component).status = ComponentStatus.DEREGISTERED
        elif any(word in message for word in ["finished", "quit", "crashed"]):
            if message.startswith("Instance"):
                _, component, _ = message.split(maxsplit=2)
                self._comp(component).status = ComponentStatus.FINISHED
                if "crashed" in message:
                    self._comp(component).exit_code = "crashed"
                else:
                    _, exit_code = message.rsplit(maxsplit=1)
                    self._comp(component).exit_code = exit_code
            else:
                # The simulation finished
                self.status = message

    def _comp(self, name: str) -> Component:
        """Instantiate if necessary and get component object for given name"""
        if name not in self.components:
            self.components[name] = Component(name)
        return self.components[name]

    def to_dataframe(self) -> pd.DataFrame:
        """Create dataframe for status table viewer, sorted by component
        name to match the log messages table"""
        rows = [
            {
                "name": component.name,
                "status": component.status,
                "exit_code": component.exit_code_message,
            }
            for component in self.components.values()
        ]
        # Pass explicit columns so set_index("name") still works when no
        # components have been parsed yet (e.g. an empty or not-yet-populated
        # manager log), instead of raising "None of ['name'] are in the columns".
        return (
            pd.DataFrame(rows, columns=["name", "status", "exit_code"])
            .set_index("name")
            .sort_index()
        )

    @property
    def simulation_state(self) -> str:
        """Overall run state derived from per-component data, one of
        'not started', 'running', 'finished' or 'failed'."""
        components = self.components.values()
        if any(c.exit_code not in ("", "0") for c in components):
            return "failed"
        if components and all(c.status is ComponentStatus.FINISHED for c in components):
            return "finished"
        if all(c.status is ComponentStatus.NOT_STARTED for c in components):
            return "not started"
        return "running"

    @property
    def status_message(self):
        if self.status:
            return self.status
        if all(
            component.status == ComponentStatus.FINISHED
            for component in self.components.values()
        ):
            return "Finished"
        elif all(
            component.status == ComponentStatus.NOT_STARTED
            for component in self.components.values()
        ):
            return "Not started"
        else:
            return "Running"
