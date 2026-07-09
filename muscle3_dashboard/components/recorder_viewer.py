"""Tabs for recorder (tap) actors: their distilled data, their plots.

A recorder actor (``imas_muscle3.actors.recorder_component``, ``distill``
format) taps a running simulation and writes one live-tailable Zarr store per
*occurrence* (outer-loop iteration) under
``instances/<rec>/workdir/<port>/<NNNN>.zarr``. When the run's settings point
the recorder at a visualization plot file (``<rec>.config``, defining the
``State``/``Plotter`` classes of ``imas_muscle3.visualization``), the same
file tells this viewer how to render the stored data: the ``State`` defined
what was recorded, the ``Plotter`` is loaded here to plot it — live while the
run appends, or after it finished.

Two more settings are honoured (both optional, read from
``configuration.ymmsl``):

- ``<rec>.md``: whitespace-separated ``ids_name=imas_uri`` pairs naming the
  static machine-description IDSs the Plotter overlays (wall, coil geometry).
- a store's root attribute ``distill_profile`` (stamped by the recorder) is
  the fallback plot-file reference when settings are unavailable.

The IMAS/plotting stack (imas, xarray, zarr, holoviews, the plot file's own
imports) is imported lazily per tab; a missing dependency degrades to a
message pane instead of breaking the dashboard.
"""

import html
import logging
import runpy
from pathlib import Path
from typing import Collection

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
        plot_file = settings.get(f"{name}.config") or _profile_from_stores(
            port_dirs
        )
        md_spec = settings.get(f"{name}.md")
        tabs.append(
            (
                name,
                RecorderViewer(
                    name,
                    port_dirs,
                    str(plot_file) if plot_file else None,
                    str(md_spec) if md_spec else None,
                ),
            )
        )
    return tabs


class RecorderViewer(Viewer):
    """One recorder instance's tab: occurrence selector + its Plotter.

    The plot file's ``Plotter`` renders a ``State`` whose ``data`` this viewer
    fills from the Zarr stores instead of live IDS messages. A periodic
    callback re-checks the stores (cheap metadata reads) and pushes grown data
    into the state, so an in-progress run plots live; 'latest' follows new
    occurrences (Picard iterations) as they appear.
    """

    def __init__(
        self,
        name: str,
        port_dirs: list[Path],
        plot_file: str | None,
        md_spec: str | None,
    ) -> None:
        self._name = name
        self._port_dirs = port_dirs
        self._plot_file = plot_file
        self._md_spec = md_spec
        self._state = None
        self._plotter = None
        self._data_key: object = None
        self._md_cache: dict | None = None

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
            self._show_message(
                f"Plot file `{html.escape(self._plot_file)}` not found."
            )
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
            self._state = state_class(self._load_md())
            self._plotter = plotter_class(self._state)
            self._plot_area.objects = [self._plotter]
        except Exception as e:
            logger.exception("loading plot file %s failed", self._plot_file)
            self._show_message(
                f"Could not load `{html.escape(str(self._plot_file))}`: "
                f"`{html.escape(repr(e))}`"
            )

    def _load_md(self) -> dict:
        """Machine-description IDSs from the ``<rec>.md`` setting."""
        if self._md_cache is not None:
            return self._md_cache
        md: dict = {}
        for entry in (self._md_spec or "").split():
            try:
                ids_name, uri = entry.split("=", 1)
                import imas

                with imas.DBEntry(uri, "r") as db:
                    md[ids_name] = db.get(ids_name)
            except Exception as e:
                logger.warning("could not load md entry '%s': %s", entry, e)
        self._md_cache = md
        return md

    def _show_message(self, text: str) -> None:
        self._plot_area.objects = [pn.pane.Markdown(text)]

    # -- data --------------------------------------------------------------

    def _occurrences(self) -> list[str]:
        """Sorted occurrence names present across this recorder's ports."""
        return sorted(
            {
                store.stem
                for port in self._port_dirs
                for store in port.glob("*.zarr")
            }
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
                    logger.debug(
                        "group %s of %s not readable yet", group, store
                    )
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
                (group, ds.sizes.get("time", 0))
                for group, ds in sorted(data.items())
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
