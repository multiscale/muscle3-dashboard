"""The domain-agnostic half of the State/Plotter contract: a ``State`` holds
whatever a workflow's recorder or live visualization actor extracted, keyed
by source name; a :class:`~.base_plotter.BasePlotter` renders it reactively.

Discovering and extracting quantities automatically (``automatic_extract``)
is domain-specific -- it has to know how to walk *some* structured message
format -- so it's left abstract here for a subclass to implement.
"""

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import param

logger = logging.getLogger()


class Dim(Enum):
    """Enum for variable dimensionality."""

    ZERO_D = "0D"
    ONE_D = "1D"
    TWO_D = "2D"


@dataclass
class Variable:
    """Represents a single discoverable variable from a source."""

    source_name: str
    path: str
    dimension: Dim
    coord_names: list[str] = field(default_factory=list)
    is_visualized: bool = False

    @property
    def full_path(self) -> str:
        """Returns the full path for UI display (source_name/path)."""
        return f"{self.source_name}/{self.path}"


class BaseState(param.Parameterized):
    """Abstract container for simulation state. Holds live simulation data,
    keyed by source name.
    """

    data = param.Dict(default={}, doc="Mapping of source name to data objects.")
    variables = param.Dict(
        default={},
        doc=("Mapping of a variable's full path to a Variable object"),
    )

    def __init__(
        self,
        init_data: dict[str, Any],
        auto: bool = False,
        extract_all: bool = False,
    ) -> None:
        super().__init__()  # type: ignore[no-untyped-call]
        self.extract_all = extract_all
        self.auto = auto
        if init_data:
            self.data = dict(init_data)
        self._discovery_done: set[str] = set()

    def extract_data(self, message: Any) -> None:
        """Extract data from a received message and store it in ``data``."""
        if self.auto:
            self.automatic_extract(message)
        self.extract(message)

    def extract(self, message: Any) -> None:
        """Extract data from a received message and store it in ``data``.
        Must be implemented by a subclass."""
        raise NotImplementedError(
            "A state class needs to implement an `extract` method"
        )

    def automatic_extract(self, message: Any) -> None:
        """Discover and extract quantities from a received message with no
        domain-specific code of its own. There's no generic way to do this
        for an arbitrary message format, so a domain-specific subclass must
        implement it before this can be used."""
        raise NotImplementedError(
            "Automatic extraction needs a domain-specific subclass "
            "implementing `automatic_extract`."
        )
