import datetime
import os
from pathlib import Path

import param

from muscle3_dashboard.constants import MAX_LINES

#: When tailing, the most bytes read from the end of the file on the first read.
#: Comfortably holds MAX_LINES of typical log output; MAX_LINES is the real
#: bound, this just caps the initial read so a huge log isn't slurped at open.
TAIL_BYTES = 256 * 1024


class BaseLogAnalyzer(param.Parameterized):
    new_lines = param.List()
    """New lines to be added"""

    def __init__(self, log_file: Path, tail: bool = False) -> None:
        super().__init__()
        self._name = log_file.parent.name
        self._path: Path = log_file
        # errors="replace": logs may hold non-UTF-8 bytes, and a tail seek can
        # land mid-character; decoding must never raise.
        self._file = log_file.open("r", errors="replace")
        if tail:
            self._seek_to_tail()
        self.update()

    def _seek_to_tail(self) -> None:
        """Skip to near the end so the first read yields only the last lines.

        Only the last MAX_LINES are ever displayed, so reading a large log in
        full at page load is wasted work. Seek to the last TAIL_BYTES and drop
        the (likely partial) first line; later reads tail incrementally from the
        file position left behind.
        """
        try:
            size = os.fstat(self._file.fileno()).st_size
        except OSError:
            return
        if size > TAIL_BYTES:
            self._file.seek(size - TAIL_BYTES)
            self._file.readline()  # discard the partial line at the seek point

    def update(self) -> None:
        """Parse new lines of log file and update parsed information"""
        log_lines = list(self._file)
        if not log_lines:
            return
        combined = self.new_lines + log_lines
        # Only the last MAX_LINES are ever shown; cap so a fast writer between
        # polls (or a full backlog) can't grow this unboundedly.
        if len(combined) > MAX_LINES:
            combined = combined[-MAX_LINES:]
        self.param.update(new_lines=combined)

    def pop_new_lines(self):
        """Get new lines from log file and reset self.new_lines"""
        popped_lines, self.new_lines = self.new_lines, []
        return popped_lines

    def file_last_updated(self) -> datetime.datetime:
        """Get last time file was modified"""
        return datetime.datetime.fromtimestamp(os.path.getmtime(self._file.name))

    @property
    def path(self):
        """Path to log file"""
        return self._path
