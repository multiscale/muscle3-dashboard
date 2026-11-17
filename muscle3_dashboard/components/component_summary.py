import html
import logging
import re
from pathlib import Path

import panel as pn

from muscle3_dashboard.constants import CARD_MARGIN
from muscle3_dashboard.data_manager import DataManager
from muscle3_dashboard.pathlink import copy_link
from muscle3_dashboard.reactive import ClickableHTML, CopyButton, encode_markup

logger = logging.getLogger(__name__)

try:
    import ymmsl
    from ymmsl.v0_2 import Configuration
except ImportError:  # ymmsl2svg not installed
    ymmsl = None

#: Largest file we'll load into the read-only viewer.
_MAX_FILE_BYTES = 512 * 1024

#: Absolute-path-looking tokens.
_PATH_RE = re.compile(r"/[^\s'\"<>():,]+")


class ComponentSummaryViewer(pn.viewable.Viewer):
    """Show a clicked component: its program / settings / description (with
    referenced text files as inline links), and a read-only viewer that opens a
    file when its link is clicked.
    """

    def __init__(self, data_manager: DataManager) -> None:
        super().__init__()
        self.data_manager = data_manager
        self._config = None
        self._loaded = False
        self._text_files: set[str] = set()

        self.details = ClickableHTML(
            html_enc=encode_markup(
                "<i>Click a component in the graph for details.</i>"
            ),
            sizing_mode="stretch_width",
        )
        self.details.param.watch(self._on_path_click, "clicked")
        self.editor = pn.widgets.CodeEditor(
            value="",
            readonly=True,
            visible=False,
            sizing_mode="stretch_width",
            height=360,
        )
        # Header above the read-only viewer: the file path as a click-to-copy
        # link, a copy-contents button, and a close button.
        self.editor_path = pn.pane.HTML("", visible=False, align="center")
        self.editor_copy = CopyButton(visible=False, align="center")
        self.editor_close = pn.widgets.Button(
            name="✕ close",
            button_type="light",
            visible=False,
            width=90,
            margin=(4, 4),
            align="center",
        )
        self.editor_close.on_click(self._close_editor)
        self.editor_header = pn.Row(
            self.editor_path,
            pn.HSpacer(),
            self.editor_copy,
            self.editor_close,
            sizing_mode="stretch_width",
        )
        self.card = pn.Card(
            pn.Column(self.details, self.editor_header, self.editor),
            title="Component",
            margin=CARD_MARGIN,
            sizing_mode="stretch_width",
            collapsible=True,
        )

    def _config_obj(self):
        if not self._loaded:
            self._loaded = True
            config = self.data_manager.run_folder / "configuration.ymmsl"
            if ymmsl is not None and config.is_file():
                try:
                    self._config = ymmsl.load_as(Configuration, config)
                except Exception as e:
                    logger.warning("Could not load %s: %s", config, e)
        return self._config

    def show(self, component_name: str) -> None:
        """Render the summary for the given (base) component name."""
        cfg = self._config_obj()
        comp = None
        if cfg is not None:
            comp = {str(c.name): c for c in cfg.root_model().components.values()}.get(
                component_name
            )
        self._close_editor()
        if comp is None:
            self.details.html_enc = encode_markup(
                f"<b>{html.escape(component_name)}</b><br>"
                "<i>Not in the configuration.</i>"
            )
            return

        program = cfg.programs.get(comp.implementation)
        self._text_files = set(_detect_files(comp, program, cfg, component_name))
        self.details.html_enc = encode_markup(
            _details_html(comp, program, cfg, component_name, self._text_files)
        )

    def _on_path_click(self, event) -> None:
        path = event.new
        self.details.clicked = ""  # reset so the same link can be clicked again
        if path and path in self._text_files:
            self._open(path)

    def _editor_widgets(self):
        """The read-only viewer widgets shown/hidden together."""
        return (self.editor, self.editor_path, self.editor_copy, self.editor_close)

    def _open(self, path: str) -> None:
        try:
            data = Path(path).read_bytes()[:_MAX_FILE_BYTES]
            text = data.decode("utf-8", errors="replace")
        except OSError as e:
            text = f"# Could not read {path}: {e}"
        # Let Ace's modelist derive the syntax mode from the filename extension.
        self.editor.filename = path
        self.editor.value = text
        # Header: the filename as a click-to-copy link for the full path, and a
        # button to copy the file's contents.
        self.editor_path.object = "<b>File:</b> " + copy_link(
            Path(path).name, Path(path)
        )
        self.editor_copy.payload_enc = encode_markup(text)
        for widget in self._editor_widgets():
            widget.visible = True

    def _close_editor(self, _event=None) -> None:
        for widget in self._editor_widgets():
            widget.visible = False

    def __panel__(self):
        return self.card


# -- details rendering ----------------------------------------------------
def _linkify(escaped_text: str, paths: set[str]) -> str:
    """Turn detected text-file paths in already-escaped text into links."""
    for path in sorted(paths, key=len, reverse=True):
        esc = html.escape(path)
        if esc in escaped_text:
            escaped_text = escaped_text.replace(
                esc,
                f'<a data-path="{esc}" style="color:#1976d2;cursor:pointer;'
                f'text-decoration:underline">{esc}</a>',
            )
    return escaped_text


def _details_html(comp, program, cfg, name: str, text_files: set[str]) -> str:
    def link(raw: str) -> str:
        return _linkify(html.escape(raw), text_files)

    sections = []
    description = (comp.description or "").strip()
    if description:
        sections.append(f"<p>{html.escape(description)}</p>")
    if program is not None:
        rows = []
        executable = getattr(program, "executable", None)
        args = list(getattr(program, "args", []) or [])
        if executable is not None:
            command = " ".join([str(executable), *args])
            rows.append(f"<div><b>Command:</b> <code>{link(command)}</code></div>")
        venv = getattr(program, "virtual_env", None)
        if venv:
            rows.append(
                f"<div><b>venv:</b> <code>{html.escape(str(venv))}</code></div>"
            )
        modules = list(getattr(program, "modules", []) or [])
        if modules:
            rows.append(
                "<div><b>Modules:</b> "
                f"<code>{html.escape(', '.join(map(str, modules)))}</code></div>"
            )
        script = getattr(program, "script", None)
        if script:
            rows.append(
                "<div><b>Script:</b><pre style='margin:2px 0;max-height:12em;"
                "overflow:auto;background:#f5f5f5;padding:4px'>"
                f"{link(str(script))}</pre></div>"
            )
        sections.append("".join(rows))
    prefix = name + "."
    settings = {
        str(key)[len(prefix) :]: value
        for key, value in cfg.settings.items()
        if str(key).startswith(prefix)
    }
    if settings:
        lines = "\n".join(
            f"  {html.escape(k)}: {link(repr(v))}," for k, v in sorted(settings.items())
        )
        sections.append(
            "<b>Settings</b><pre style='margin:2px 0;white-space:pre-wrap'>"
            f"{{\n{lines}\n}}</pre>"
        )
    return "".join(sections)


# -- file detection -------------------------------------------------------
def _is_text_file(path: Path) -> bool:
    """Heuristic: a readable file with no NUL byte in its first 4 KiB."""
    try:
        with path.open("rb") as f:
            return b"\x00" not in f.read(4096)
    except OSError:
        return False


def _detect_files(comp, program, cfg, name: str) -> list[str]:
    """Existing *text* files referenced by the program and settings."""
    blobs: list[str] = []
    if program is not None:
        blobs.append(str(getattr(program, "executable", "") or ""))
        blobs.extend(str(a) for a in getattr(program, "args", []) or [])
        blobs.append(str(getattr(program, "script", "") or ""))
    prefix = name + "."
    for key, value in cfg.settings.items():
        if str(key).startswith(prefix):
            blobs.append(str(value))
    found: dict[str, None] = {}
    for blob in blobs:
        for match in _PATH_RE.findall(blob):
            if match not in found:
                p = Path(match)
                if p.is_file() and _is_text_file(p):
                    found[match] = None
    return list(found)
