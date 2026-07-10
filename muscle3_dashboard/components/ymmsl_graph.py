import html
import logging
import time
from collections.abc import Callable

import panel as pn
import param

from muscle3_dashboard.constants import CARD_MARGIN, SHOW_PORT_ICONS
from muscle3_dashboard.data_manager import DataManager
from muscle3_dashboard.instances import base_name
from muscle3_dashboard.loganalyzer.manager import ComponentStatus
from muscle3_dashboard.reactive import encode_markup

logger = logging.getLogger(__name__)

#: MUSCLE3 docs section ("Designing the connections") with the gMMSL diagram
#: explaining how conduits connect the port operators (F_INIT / O_I / S / O_F)
#: across time scales — the annotations whose mismatch makes ymmsl2svg's strict
#: timeline check fail.
_TIMELINE_DOCS_URL = (
    "https://muscle3.readthedocs.io/en/latest/tutorial.html#designing-the-connections"
)

# Component box fill per status bucket. The same colour is drawn fully opaque for
# an active component (wrote log output since the last render) and dimmed to
# _IDLE_OPACITY for an idle one, so currently-active components stand out while
# every box keeps its status hue. Light tints keep black labels readable.
# muscle3 has no distinct "running" status -- REGISTERED is the active state.
# "crashed" is the likely culprit (a real non-zero exit); "killed" is collateral
# (SIGKILL -9 / generic crash when another component failed first).
_STATUS_FILL = {
    "not_started": "#eeeeee",  # grey: not started / starting up
    "running": "#bbdefb",  # blue: registered / running
    "finished": "#c8e6c9",  # green: finished / deregistered
    "killed": "#ffcdd2",  # pale red: collateral crash
    "crashed": "#ef9a9a",  # red: responsible / likely culprit
}
#: Fill-opacity for an idle component (active ones are fully opaque).
_IDLE_OPACITY = 0.4
#: A component stays highlighted (opaque) for this long after its last log write,
#: so it doesn't flicker between writes.
_ACTIVE_WINDOW_SECONDS = 10.0
# (stroke colour, width) overlaid on crash buckets so the culprit stands out most.
_CRASH_STROKE = {"crashed": ("#b71c1c", 5), "killed": ("#e57373", 2)}
_RUNNING_STATUSES = {
    ComponentStatus.PLANNED,
    ComponentStatus.INSTANTIATING,
    ComponentStatus.REGISTERED,
}
_FINISHED_STATUSES = {ComponentStatus.DEREGISTERED, ComponentStatus.FINISHED}
# When a multiplicity's instances differ, show the most attention-worthy state.
_BUCKET_PRIORITY = {
    "crashed": 4,
    "killed": 3,
    "running": 2,
    "not_started": 1,
    "finished": 0,
}
# Legend, in the order shown. Label per status bucket for the colour key.
_STATUS_LABEL = {
    "not_started": "not started",
    "running": "running",
    "finished": "finished",
    "killed": "killed (collateral)",
    "crashed": "crashed (likely cause)",
}


def _legend_html() -> str:
    """A compact colour key for the status fills, outlines, and opacity, so a
    reader can map a box's colour to a component state and spot crash suspects."""
    swatches = []
    for bucket, label in _STATUS_LABEL.items():
        stroke = _CRASH_STROKE.get(bucket)
        border = (
            f"border:{min(stroke[1], 2)}px solid {stroke[0]}"
            if stroke
            else "border:1px solid #bbb"
        )
        swatches.append(
            '<span style="display:inline-flex;align-items:center;gap:4px;'
            'margin-right:10px;white-space:nowrap">'
            f'<span style="width:14px;height:14px;border-radius:2px;'
            f'background:{_STATUS_FILL[bucket]};{border}"></span>{label}</span>'
        )
    swatches.append(
        '<span style="white-space:nowrap;opacity:0.8">'
        "solid = active (recent log output), faded = idle</span>"
    )
    return (
        '<div style="display:flex;flex-wrap:wrap;align-items:center;'
        'font-size:0.78em;opacity:0.85;margin:2px 4px 0">'
        + "".join(swatches)
        + "</div>"
    )


try:
    from ymmsl import load_as
    from ymmsl.v0_2 import Configuration, TimelineTree
    from ymmsl2svg.main import configuration2svg
    from ymmsl2svg.settings import settings as ymmsl2svg_settings
except ImportError:  # optional "graph" extra not installed
    configuration2svg = None
    ymmsl2svg_settings = None


class _ClickableSVG(pn.reactive.ReactiveHTML):
    """Render an SVG string and report the clicked component.

    The SVG is **percent-encoded** (``svg_enc``): Panel runs every string param
    through an HTML sanitizer that strips ``<svg>``/``<path>``/``<style>``
    outright, so a raw SVG param renders as an empty box. Percent-encoded text
    has no tags to strip, so it passes through; the script decodes it with the
    native (UTF-8 safe, for the ``→`` in conduit labels) ``decodeURIComponent``
    and injects it as innerHTML.

    ymmsl2svg gives each component box ``id="component-<name>"``. A click on a
    box sets ``component`` to ``<name>`` (label text has ``pointer-events:none``
    so clicks fall through to the box); ``component`` is reset to "" after each
    click so the same box can be clicked again.
    """

    svg_enc = param.String(default="")
    component = param.String(default="")

    _template = (
        '<div id="wrap" style="position:relative;width:100%">'
        '<div id="graph" onclick="${script(\'click\')}" '
        "onmousemove=\"${script('hover')}\" onmouseleave=\"${script('leave')}\" "
        'style="width:100%;overflow:auto"></div>'
        '<div id="tip" style="position:fixed;display:none;pointer-events:none;'
        "background:#222;color:#fff;padding:2px 6px;border-radius:3px;"
        'font:12px sans-serif;z-index:1000;white-space:nowrap"></div>'
        "</div>"
    )
    # Decode the SVG into the div, then move every <title> into a data-tip
    # attribute and drop it: native SVG <title> tooltips have a fixed ~1s browser
    # delay, so we render our own instant tooltip from data-tip.
    _draw = (
        "graph.innerHTML = data.svg_enc ? decodeURIComponent(data.svg_enc) : '';"
        "graph.querySelectorAll('title').forEach(t => {"
        "  if (t.parentNode) { t.parentNode.setAttribute('data-tip', t.textContent); }"
        "  t.remove();"
        "});"
    )
    _scripts = {
        "render": _draw,
        "svg_enc": _draw,
        "click": (
            "const el = state.event.target.closest('[id^=\"component-\"]');"
            "if (el) { data.component = el.id.slice('component-'.length); }"
        ),
        "hover": (
            "const e = state.event;"
            "const el = e.target.closest('[data-tip]');"
            "if (el) {"
            "  tip.textContent = el.getAttribute('data-tip');"
            "  tip.style.left = (e.clientX + 12) + 'px';"
            "  tip.style.top = (e.clientY + 12) + 'px';"
            "  tip.style.display = 'block';"
            "} else { tip.style.display = 'none'; }"
        ),
        "leave": "tip.style.display = 'none'",
    }


class YmmslGraphViewer(pn.viewable.Viewer):
    """Render the run's coupling diagram from its ``configuration.ymmsl``.

    Visualization needs the optional ``ymmsl2svg`` dependency
    (``pip install muscle3-dashboard[graph]``); without it, a missing config,
    or an unvisualizable model, a short message and a component dropdown are
    shown instead. Component boxes are coloured by status (crashed also
    outlined); clicking a component (or picking it from the dropdown) invokes
    ``on_select`` with its name to show that component's logs. ymmsl2svg is
    called at most once per run -- the diagram is static, so later ticks only
    re-colour the cached SVG rather than re-rendering it.
    """

    def __init__(
        self,
        data_manager: DataManager,
        on_select: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__()
        self.data_manager = data_manager
        self.on_select = on_select
        self._base_svg: str | None = None  # rendered graph, before styling
        self._rendered = False
        # ymmsl2svg has been called for good (drawn, or definitively failed) and
        # must not be retried each tick; the config is static once present.
        self._svg_done = False
        self._styled: dict[str, tuple[str, float]] = {}  # name -> (status, opacity)
        # base name -> monotonic time its log was last seen to change, and
        # instance -> last-seen mtime, for the activity glow.
        self._last_write: dict[str, float] = {}
        self._seen_mtime: dict[str, float] = {}

        self.graph = _ClickableSVG()
        self.graph.param.watch(self._on_click, "component")
        # Small, click-to-expand "approximate layout" warning (<details>).
        self.note = pn.pane.HTML(visible=False, sizing_mode="stretch_width")
        # Colour key for the status fills; shown only alongside the graph.
        self.legend = pn.pane.HTML(
            _legend_html(), visible=False, sizing_mode="stretch_width"
        )
        self.message = pn.pane.Markdown(visible=False)  # error / no-graph text
        # Fallback when there is no graph: a dropdown so every component's log is
        # still reachable (the manager log has its own button on the log card).
        self.fallback_select = pn.widgets.Select(
            name="Show a component log",
            options=[],
            visible=False,
            width=260,
            margin=(4, 4),
        )
        self.fallback_select.param.watch(self._on_fallback_select, "value")
        self.card = pn.Card(
            self.note,
            self.graph,
            self.legend,
            self.message,
            self.fallback_select,
            title="Simulation graph",
            sizing_mode="stretch_width",
            margin=CARD_MARGIN,
        )
        self.data_manager.param.watch(self.update, "data_updated")
        # Generating the SVG (ymmsl2svg) is the single most expensive step in
        # building the run page, so defer it until after first paint: show a
        # placeholder now and render in an onload callback, so the page shell
        # appears immediately and the graph fills in a moment later. Outside a
        # live session (scripts/tests) there is no onload, so render now.
        self._show_message("Rendering simulation graph…")
        if pn.state.curdoc is not None:
            pn.state.onload(self.render)
        else:
            self.render()

    def render(self) -> None:
        """Render configuration.ymmsl into the graph, with fallbacks."""
        if configuration2svg is None:
            # A missing dependency won't change while we run: settle for good.
            self._svg_done = True
            self._show_message(
                "Install the optional `ymmsl2svg` dependency to see the "
                "simulation graph: `pip install muscle3-dashboard[graph]`."
            )
            return

        config = self.data_manager.run_folder / "configuration.ymmsl"
        if not config.is_file():
            # The config may yet be written; keep polling (cheaply) for it.
            self._show_message(f"No `configuration.ymmsl` in `{config.parent}`.")
            return

        # From here the config exists and is static, so whatever we make of it is
        # final: draw (or fail) once and never call it again.
        self._svg_done = True

        # Parsing the ymmsl file dominates the render cost (~40 ms vs ~4 ms to
        # draw), so parse it once and reuse it. The drawing is identical whether
        # or not timelines validate — the strict check only raises, it doesn't
        # change the layout — so always draw non-strict and verify the timelines
        # separately and cheaply (~1 ms), instead of drawing a second time when
        # the strict check fails.
        try:
            cfg = load_as(Configuration, config)
        except Exception as load_error:
            logger.warning("Could not load %s: %s", config, load_error)
            self._show_message(f"Could not load `{config.name}`: {load_error}")
            return

        ymmsl2svg_settings.debug = False
        ymmsl2svg_settings.check_timelines = False
        # hasattr-guarded so an older ymmsl2svg without the option still renders.
        if not SHOW_PORT_ICONS and hasattr(ymmsl2svg_settings, "disable_port_icons"):
            ymmsl2svg_settings.disable_port_icons()
        try:
            svg = configuration2svg(cfg)
        except Exception as draw_error:
            logger.warning("Could not visualize %s: %s", config, draw_error)
            self._show_message(f"Could not visualize `{config.name}`: {draw_error}")
            return

        # Timelines that don't validate (e.g. time-scale bridges / accumulators)
        # still draw fine, but the layout is approximate — flag it.
        note_html = ""
        try:
            TimelineTree(cfg.root_model()).check_consistent()
        except Exception as timeline_error:
            note_html = self._approximate_note(timeline_error)

        self._base_svg = str(svg)
        self._rendered = True
        self.graph.visible = True
        self.legend.visible = True
        self.fallback_select.visible = False
        self.message.visible = False
        self.note.object = note_html
        self.note.visible = bool(note_html)
        self._apply_styling()

    @staticmethod
    def _approximate_note(error: Exception) -> str:
        """Small, click-to-expand notice carrying the full strict error, with a
        link to the docs explaining the timeline/operator annotations."""
        details = html.escape(str(error))
        return (
            '<details style="font-size:0.8em;opacity:0.8;margin:2px 4px">'
            '<summary style="cursor:pointer">&#9888; Timelines could not be '
            "verified — layout is approximate (click for details)</summary>"
            '<pre style="white-space:pre-wrap;font-size:0.95em;margin:4px 0">'
            f"{details}</pre>"
            '<div style="font-size:0.95em">See the MUSCLE3 docs on '
            f'<a href="{_TIMELINE_DOCS_URL}" target="_blank" rel="noopener">'
            "designing the connections</a> (the F_INIT / O_I / S / O_F operators "
            "and conduits) for how component timelines are derived and annotated."
            "</div></details>"
        )

    def _bucket(self, component) -> str:
        """Map a component's status / exit code to a colour bucket."""
        crash = component.crash_kind  # "culprit" | "killed" | None (structured)
        if crash == "culprit":
            return "crashed"  # likely root cause: ranked highest, strong red
        if crash == "killed":
            return "killed"  # collateral SIGKILL / generic crash: pale red
        if component.status in _RUNNING_STATUSES:
            return "running"
        if component.status in _FINISHED_STATUSES:
            return "finished"
        return "not_started"

    def _component_statuses(self) -> dict[str, str]:
        """Base component name -> status bucket for colouring.

        Strips a multiplicity suffix (``worker[0]`` -> ``worker``) so instances
        map to the single box ymmsl2svg draws, keeping the highest-priority
        bucket when a multiplicity's instances differ.
        """
        best: dict[str, str] = {}
        components = self.data_manager.manager_log_analyzer.components
        for name, component in components.items():
            base = base_name(name)
            bucket = self._bucket(component)
            current = best.get(base)
            if current is None or _BUCKET_PRIORITY[bucket] > _BUCKET_PRIORITY[current]:
                best[base] = bucket
        return best

    def _active_components(self) -> set[str]:
        """Base component names whose log changed within _ACTIVE_WINDOW_SECONDS.

        Activity is detected from log-file mtimes (a cheap stat surfaced by the
        data manager), not from log *content*, so it stays correct even if
        reading log bodies becomes lazy. The glow window is measured on the
        local monotonic clock — recording when a change was first observed — so
        it is unaffected by NFS server/client skew in the mtime values.
        """
        now = time.monotonic()
        for instance, mtime in self.data_manager.instance_mtimes.items():
            if self._seen_mtime.get(instance) != mtime:
                self._seen_mtime[instance] = mtime
                self._last_write[base_name(instance)] = now
        return {
            name
            for name, written in self._last_write.items()
            if now - written <= _ACTIVE_WINDOW_SECONDS
        }

    def _component_styles(self) -> dict[str, tuple[str, float]]:
        """Base component name -> (status bucket, fill opacity).

        A component is fully opaque while active (wrote log output within the
        last _ACTIVE_WINDOW_SECONDS) and dimmed to _IDLE_OPACITY otherwise.
        """
        active = self._active_components()
        styles: dict[str, tuple[str, float]] = {}
        for name, bucket in self._component_statuses().items():
            opacity = 1.0 if name in active else _IDLE_OPACITY
            styles[name] = (bucket, opacity)
        return styles

    def _apply_styling(
        self, styles: dict[str, tuple[str, float]] | None = None
    ) -> None:
        """Inject styling (clickable boxes + status colour / activity) into the SVG.

        The fill is the status colour, dimmed via fill-opacity when idle.
        Crashed/killed components also get a red outline. Attribute selectors
        (not #id) so component names with '.'/'[' don't break the CSS.
        """
        if self._base_svg is None:
            return
        if styles is None:
            styles = self._component_styles()
        css = "text{pointer-events:none}[id^='component-']{cursor:pointer}"
        for name, (bucket, opacity) in sorted(styles.items()):
            rule = (
                f'[id="component-{name}"]'
                f"{{fill:{_STATUS_FILL[bucket]};fill-opacity:{opacity}"
            )
            stroke = _CRASH_STROKE.get(bucket)
            if stroke:
                rule += f";stroke:{stroke[0]};stroke-width:{stroke[1]}"
            css += rule + "}"
        styled = self._base_svg.replace("</svg>", f"<style>{css}</style></svg>", 1)
        self.graph.svg_enc = encode_markup(styled)
        self._styled = styles

    def _on_click(self, event) -> None:
        component = event.new
        if not component:
            return
        # Allow re-clicking the same component (reset before dispatch).
        self.graph.component = ""
        if self.on_select is not None:
            self.on_select(component)

    def _on_fallback_select(self, event) -> None:
        component = event.new
        # Reset so re-selecting the same component fires again.
        self.fallback_select.value = None
        if component and self.on_select is not None:
            self.on_select(component)

    def _selectable_components(self) -> list[str]:
        """Base component names known from the manager log or the instance log
        dirs — the set whose logs are reachable without a graph."""
        names = {
            base_name(name)
            for name in self.data_manager.manager_log_analyzer.components
        }
        names.update(base_name(name) for name in self.data_manager.stdout_log_analyzers)
        return sorted(names)

    def _show_message(self, text: str) -> None:
        """Show ``text`` instead of the graph, plus a dropdown of components so
        their logs stay reachable (the manager log has its own button on the
        log card)."""
        self.graph.visible = False
        self.legend.visible = False
        self.note.visible = False
        self.message.object = text
        self.message.visible = True
        self._populate_fallback()

    def _populate_fallback(self) -> None:
        """Refresh the no-graph component dropdown from the components known so
        far; cheap, so it can run each tick while components are discovered."""
        components = self._selectable_components()
        # Keep None as the placeholder value so the watcher only fires on a real
        # pick; options is a dict so the visible label is the component name.
        options = {"— select —": None}
        options.update({c: c for c in components})
        if list(self.fallback_select.options) == list(options):
            return  # unchanged; avoid resetting the user's open dropdown
        self.fallback_select.options = options
        self.fallback_select.value = None
        self.fallback_select.visible = bool(components)

    def update(self, event) -> None:
        if not self._svg_done:
            self.render()  # draws the graph or shows the message + dropdown
            return
        if self._rendered:
            # The diagram is static; only statuses / recent activity evolve, so
            # re-colour the cached SVG when the sampled styling changes.
            styles = self._component_styles()
            if styles != self._styled:
                self._apply_styling(styles)
        else:
            # No graph and we won't retry ymmsl2svg; keep the dropdown current
            # as components appear in the log.
            self._populate_fallback()

    def __panel__(self):
        return self.card
