"""Dashboard tabs for recorder (tap) actors: discover their data, plot it.

A recorder actor (``distill`` format) writes one live-tailable Zarr store per
occurrence (outer-loop iteration) under
``instances/<rec>/workdir/<port>/<NNNN>.zarr``. This module finds those
stores and builds a :class:`RecorderViewer` tab per recorder instance that
renders them, live or after the run finishes.

Each tab is rendered by a *plot file* defining a ``State`` class (what was
recorded) and a ``Plotter`` class (how to draw it). It's located via, in
order: the recorder's own snapshot of it (``workdir/<plot file name>``,
copied at start-up so the tab always matches the code that recorded the
data), or else ``configuration.ymmsl`` (the ``<rec>.config`` setting, or a
store's ``distill_profile`` attribute).

If the plot file defines ``init_state(settings) -> dict``, it's called with
the recorder's ``<rec>.*`` settings (prefix stripped) to build the dict
passed to ``State``'s constructor — the plot file's own chance to turn
settings into whatever ``State`` expects. Otherwise ``State`` gets an empty
dict.

The plotting stack (xarray, zarr, holoviews, the plot file's own imports) is
imported lazily per tab, so a missing dependency degrades one tab to a
message pane rather than breaking the dashboard.
"""

import html
import logging
import runpy
from collections.abc import Collection
from pathlib import Path

import panel as pn
from panel.viewable import Viewer

from muscle3_dashboard.panel_util import add_session_periodic_callback

logger = logging.getLogger(__name__)

#: How often a recorder tab checks its stores for new data (ms). The stores
#: are append-only Zarr, so a check is a metadata read, not a data load.
_REFRESH_MS = 2000


def find_recorder_instances(run_folder: Path) -> dict[str, list[Path]]:
    """Map recorder instance name -> its port dirs holding occurrence stores.

    A recorder is recognised by its on-disk footprint — ``workdir/<port>/``
    dirs containing ``<NNNN>.zarr`` stores — so discovery needs no ymmsl and
    also works for crashed or still-starting runs.
    """
    recorders: dict[str, list[Path]] = {}
    instances = run_folder / "instances"
    if not instances.is_dir():
        return recorders
    for instance in sorted(instances.iterdir()):
        workdir = instance / "workdir"
        if not workdir.is_dir():
            continue
        port_dirs = [
            port
            for port in sorted(workdir.iterdir())
            if port.is_dir() and any(port.glob("*.zarr"))
        ]
        if port_dirs:
            recorders[instance.name] = port_dirs
    return recorders


def _read_settings(run_folder: Path) -> dict[str, object]:
    """Flat settings from the run's ``configuration.ymmsl`` (empty if
    unavailable — e.g. the optional ymmsl dependency is not installed)."""
    config = run_folder / "configuration.ymmsl"
    if not config.is_file():
        return {}
    try:
        import ymmsl
        from ymmsl.v0_2 import Configuration

        cfg = ymmsl.load_as(Configuration, config)
        return {str(name): value for name, value in cfg.settings.items()}
    except Exception as e:
        logger.warning("Could not read settings from %s: %s", config, e)
        return {}


def _profile_from_stores(port_dirs: list[Path]) -> str | None:
    """The ``distill_profile`` stamped on any of this recorder's stores."""
    try:
        import zarr
    except ImportError:
        return None
    for port in port_dirs:
        for store in sorted(port.glob("*.zarr")):
            try:
                root = zarr.open_group(str(store), mode="r")
                profile = root.attrs.get("distill_profile")
                if profile:
                    return str(profile)
            except Exception:
                continue
    return None


def _resolve_plot_file(port_dirs: list[Path], plot_file: object) -> str | None:
    """Prefer the recorder's snapshot of the plot file, saved next to its
    data: it is the code that produced the stores, immune to later edits of
    the original. Fall back to the reference as given."""
    if not plot_file:
        return None
    snapshot = port_dirs[0].parent / Path(str(plot_file)).name
    if snapshot.is_file():
        return str(snapshot)
    return str(plot_file)


def recorder_tabs(
    run_folder: Path, skip: Collection[str] = ()
) -> list[tuple[str, Viewer]]:
    """One ``(title, viewer)`` per recorder instance found in the run.

    ``skip`` names instances that already have a tab, so a caller polling for
    recorders that appear mid-run only builds viewers for new ones.
    """
    recorders = {
        name: port_dirs
        for name, port_dirs in find_recorder_instances(run_folder).items()
        if name not in skip
    }
    if not recorders:
        return []
    settings = _read_settings(run_folder)
    tabs = []
    for name, port_dirs in recorders.items():
        prefix = f"{name}."
        own_settings = {
            key[len(prefix) :]: value
            for key, value in settings.items()
            if key.startswith(prefix)
        }
        plot_file = own_settings.get("config") or _profile_from_stores(port_dirs)
        tabs.append(
            (
                name,
                RecorderViewer(
                    name,
                    port_dirs,
                    _resolve_plot_file(port_dirs, plot_file),
                    own_settings,
                ),
            )
        )
    return tabs


class RecorderViewer(Viewer):
    """One recorder instance's tab: occurrence selector + its Plotter.

    The plot file's ``Plotter`` renders a ``State`` whose ``data`` this viewer
    fills from the Zarr stores instead of live messages. A periodic callback
    re-checks the stores (cheap metadata reads) and pushes grown data into
    the state, so an in-progress run plots live; 'latest' follows new
    occurrences (Picard iterations) as they appear.
    """

    def __init__(
        self,
        name: str,
        port_dirs: list[Path],
        plot_file: str | None,
        settings: dict[str, object],
    ) -> None:
        self._name = name
        self._port_dirs = port_dirs
        self._plot_file = plot_file
        self._settings = settings
        self._state = None
        self._plotter = None
        self._data_key: object = None

        self.occurrence_select = pn.widgets.Select(
            name="Occurrence (outer-loop iteration)",
            options=["latest"],
            value="latest",
            width=250,
        )
        self.occurrence_select.param.watch(self._on_occurrence_change, "value")
        self.status_pane = pn.pane.Markdown(sizing_mode="stretch_width")
        self._plot_area = pn.Column(sizing_mode="stretch_width")
        self._panel = pn.Column(
            pn.Row(self.occurrence_select, self.status_pane),
            self._plot_area,
            sizing_mode="stretch_width",
        )

        self._build_plotter()
        self._refresh()
        add_session_periodic_callback(self._refresh, _REFRESH_MS)

    # -- construction ------------------------------------------------------

    def _build_plotter(self) -> None:
        """Load State/Plotter from the plot file; degrade to a message."""
        if not self._plot_file:
            self._show_message(
                "No plot file configured for this recorder. Set "
                f"`{self._name}.config` to a visualization file defining "
                "`State` and `Plotter` classes."
            )
            return
        if not Path(self._plot_file).is_file():
            self._show_message(f"Plot file `{html.escape(self._plot_file)}` not found.")
            return
        try:
            # Plot files build holoviews objects at class-definition time and
            # need a plotting backend registered first, exactly like the live
            # visualization actor does.
            import holoviews as hv

            hv.extension("bokeh")
            namespace = runpy.run_path(self._plot_file)
            state_class = namespace["State"]
            plotter_class = namespace["Plotter"]
            init_state = namespace.get("init_state")
            context = self._build_state_context(init_state)
            self._state = state_class(context)
            self._plotter = plotter_class(self._state)
            self._plot_area.objects = [self._plotter]
        except Exception as e:
            logger.exception("loading plot file %s failed", self._plot_file)
            self._show_message(
                f"Could not load `{html.escape(str(self._plot_file))}`: "
                f"`{html.escape(repr(e))}`"
            )

    def _build_state_context(self, init_state) -> dict:
        """This recorder's settings, turned into ``State``'s constructor
        argument via the plot file's own ``init_state``, if it defines one.
        No domain knowledge lives here: this viewer only forwards settings
        it already reads for its own purposes (e.g. ``config``)."""
        if init_state is None:
            return {}
        try:
            return dict(init_state(self._settings))
        except Exception:
            logger.exception(
                "init_state(%r) failed for %s", self._settings, self._plot_file
            )
            return {}

    def _show_message(self, text: str) -> None:
        self._plot_area.objects = [pn.pane.Markdown(text)]

    # -- data --------------------------------------------------------------

    def _occurrences(self) -> list[str]:
        """Sorted occurrence names present across this recorder's ports."""
        return sorted(
            {store.stem for port in self._port_dirs for store in port.glob("*.zarr")}
        )

    def _load_data(self, occurrence: str) -> dict:
        """``group -> Dataset`` for one occurrence, across all port stores.

        Datasets are opened lazily (no dask): the state holds views into the
        stores and the plots pull values on render, so a check-and-push cycle
        stays cheap.
        """
        import xarray as xr
        import zarr

        data: dict = {}
        for port in self._port_dirs:
            store = port / f"{occurrence}.zarr"
            if not store.is_dir():
                continue
            try:
                root = zarr.open_group(str(store), mode="r")
                groups = list(root.group_keys())
            except Exception:
                continue  # store being created right now
            for group in groups:
                try:
                    data[group] = xr.open_zarr(
                        store, group=group, consolidated=False, chunks=None
                    )
                except Exception:
                    logger.debug("group %s of %s not readable yet", group, store)
        return data

    # -- refresh loop --------------------------------------------------------

    def _on_occurrence_change(self, event) -> None:
        self._data_key = None  # force a reload of the newly selected store
        self._refresh()

    def _refresh(self) -> None:
        """Re-check the stores; push data into the state only when it grew."""
        occurrences = self._occurrences()
        if not occurrences:
            self.status_pane.object = "*No data recorded yet.*"
            return
        options = ["latest"] + occurrences
        if self.occurrence_select.options != options:
            self.occurrence_select.options = options

        selected = self.occurrence_select.value
        occurrence = occurrences[-1] if selected == "latest" else selected
        if self._state is None:
            self.status_pane.object = (
                f"*Occurrence {occurrence}: stores on disk, no plotter.*"
            )
            return

        data = self._load_data(occurrence)
        key = (
            occurrence,
            tuple(
                (group, ds.sizes.get("time", 0)) for group, ds in sorted(data.items())
            ),
        )
        if key == self._data_key:
            return
        self._data_key = key
        self.status_pane.object = f"*Showing occurrence {occurrence}.*"
        self._state.data = data
        self._state.param.trigger("data")

    def __panel__(self):
        return self._panel
