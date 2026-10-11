"""The supervisor's window: header, resource and process tables, and the rolling log.

The only module that imports ``tkinter`` or ``ttkbootstrap``, and only ``__main__`` imports it,
inside a function, so the package still imports on a machine without Tk.

- **Read-only.** No Actions menu and no restart buttons yet (stage 3 part 4).
- **The Tk mainloop owns the main thread**; the supervisor loop runs on a worker. No worker
  touches a widget: log lines cross on the ``DequeHandler`` deque, state on the snapshot the
  loop publishes by whole-reference assignment, and every tick redraws from those.
- **No Quit.** Closing the window minimizes it; the supervisor's lifetime is its console's
  (Ctrl-C) until the service owns it. A ``TclError`` or a missing Tk never stops the
  supervisor: :func:`open_window` returns None and the caller runs headless.
- **ttkbootstrap is optional.** Every widget is plain ``ttk``, which ttkbootstrap's theme styles
  anyway; it supplies only the themed root, its colors and the ``ScrolledText``. Without it the
  window is plain Tk: a theme is not worth a supervisor that will not start.

Design: mast-claude-config ``plans/supervisor-design.md`` §7, *The window*.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import tkinter
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from tkinter import TclError, font, ttk
from tkinter.scrolledtext import ScrolledText
from typing import cast

from common.mast_logging import get_logger
from supervision.gui_model import (
    CLOSE_HINT,
    LEVELS,
    PROCESS_COLUMNS,
    RESOURCE_COLUMNS,
    TableRow,
    about_text,
    drain,
    header,
    help_text,
    initial_level,
    process_rows,
    resource_rows,
)
from supervision.heartbeat import NO_PROCESSES_YET, NOT_YET_PROBED
from supervision.logsink import LogLine
from supervision.state import Severity, SupervisorSnapshot

logger = get_logger(__name__)


def _ttkbootstrap_loads() -> bool:
    """Imported here, once: it imports Pillow, whose native parts can fail to load."""
    try:
        import ttkbootstrap  # noqa: F401
    except ImportError as e:
        logger.warning(f"ttkbootstrap does not load ({e}); the window is plain Tk")
        return False
    return True


THEMED = _ttkbootstrap_loads()

TITLE = "mast-supervisor"
THEME = "bootstrap-dark"  # a dome at night
MIN_SIZE = (900, 600)
HEADLINE_POINTS = 16
LOG_FONT_POINTS = 10
REFRESH_MS = 1000
LOG_PANE_LINES = 12  # its natural height; it stretches to fill the window
PAD = 8  # around the window's sections
PAD_INNER = 4  # inside a section, and between its controls
PAD_WIDE = 12  # beside a headline value, and around a dialog's content
LEVEL_BOX_CHARS = 10
ROW_SPACING = 1.4  # Treeview row height, in the font's line heights
# Column widths in characters of the table font, so they scale with ttkbootstrap's high-DPI fonts.
COLUMN_CHARS = {"state": 14, "mode": 14, "detail": 32, "since": 10, "pid": 7, "restarts": 8}
DEFAULT_COLUMN_CHARS = 10
# Tag names for each severity: ttkbootstrap's semantic color names, colored from its theme;
# plain Tk falls back to named colors.
SEVERITY_STYLE = {Severity.OK: "success", Severity.WARNING: "warning", Severity.ERROR: "danger"}
FALLBACK_COLOR = {Severity.OK: "dark green", Severity.WARNING: "dark orange", Severity.ERROR: "red"}
LEVEL_SEVERITY = {
    logging.DEBUG: Severity.OK,
    logging.INFO: Severity.OK,
    logging.WARNING: Severity.WARNING,
    logging.ERROR: Severity.ERROR,
    logging.CRITICAL: Severity.ERROR,
}


@dataclass(frozen=True, slots=True)
class WindowSettings:
    """``GuiConfig``'s fields, passed in so this module does not import ``common.config``."""

    drain_interval_ms: int
    drain_max_records_per_tick: int
    log_pane_max_lines: int
    log_level: str


@dataclass(frozen=True, slots=True)
class WindowContext:
    """What the window shows that is not in the snapshot."""

    version: str
    top: Path
    log_file: Path | None
    readme: Path


def open_window(
    snapshot: Callable[[], SupervisorSnapshot],
    lines: deque[LogLine],
    settings: WindowSettings,
    context: WindowContext,
    now: Callable[[], datetime],
) -> SupervisorWindow | None:
    """The window, or None when this machine cannot show one; never raises for that."""
    try:
        return SupervisorWindow(snapshot, lines, settings, context, now)
    except TclError as e:
        logger.warning(f"no window ({e}); running headless")
    except Exception:  # noqa: BLE001 -- by design: a window that cannot be built never stops the supervisor
        logger.exception("no window: building it failed; running headless")
    return None


class _TextPane:
    """:class:`~supervision.gui_model.LogPane` over a read-only Tk ``Text``."""

    def __init__(self, text: tkinter.Text, autoscroll: tkinter.BooleanVar) -> None:
        self._text = text
        self._autoscroll = autoscroll

    def append_line(self, line: LogLine) -> None:
        tag = SEVERITY_STYLE[LEVEL_SEVERITY.get(line.levelno, Severity.ERROR)]
        self._text.configure(state="normal")
        self._text.insert("end", line.text + "\n", tag)
        self._text.configure(state="disabled")
        if self._autoscroll.get():
            self._text.see("end")

    def line_count(self) -> int:
        return int(self._text.index("end-1c").split(".")[0]) - 1

    def delete_oldest(self, count: int) -> None:
        self._text.configure(state="normal")
        self._text.delete("1.0", f"{count + 1}.0")
        self._text.configure(state="disabled")


class SupervisorWindow:
    def __init__(
        self,
        snapshot: Callable[[], SupervisorSnapshot],
        lines: deque[LogLine],
        settings: WindowSettings,
        context: WindowContext,
        now: Callable[[], datetime],
    ) -> None:
        self._snapshot = snapshot
        self._lines = lines
        self._settings = settings
        self._context = context
        self._now = now
        self._shown: SupervisorSnapshot | None = None
        if THEMED:
            import ttkbootstrap
            from ttkbootstrap.style.theme import Colors

            app = ttkbootstrap.App(title=TITLE, theme=THEME, minsize=MIN_SIZE)
            colors = cast(Colors, app.style.colors)  # the property is unannotated; this is its runtime type
            self._colors = {Severity.OK: colors.success, Severity.WARNING: colors.warning, Severity.ERROR: colors.danger}
            self._background: str | None = colors.bg
            self.root: tkinter.Tk = app
        else:
            self.root = tkinter.Tk()
            self.root.title(TITLE)
            self.root.minsize(*MIN_SIZE)
            self._colors = dict(FALLBACK_COLOR)
            self._background = None
        self.root.protocol("WM_DELETE_WINDOW", self._minimize)
        self._build_menu()
        self._build_header()
        self._resources = self._build_table("Resources", RESOURCE_COLUMNS)
        self._processes = self._build_table("Processes", PROCESS_COLUMNS)
        self._build_log()
        # After the widgets exist: ttkbootstrap builds a widget's style when it is created.
        linespace = font.nametofont("TkDefaultFont").metrics("linespace")
        style = ttk.Style(self.root)
        style.configure("Treeview", rowheight=round(linespace * ROW_SPACING))
        if self._background is not None:  # a table's empty rows are otherwise left unthemed
            style.configure("Treeview", fieldbackground=self._background)

    # --- building ----------------------------------------------------------------------------

    def _build_menu(self) -> None:
        bar = tkinter.Menu(self.root)
        bar.add_command(label="About", command=self._about)
        bar.add_command(label="Help", command=self._help)
        self.root.configure(menu=bar)

    def _build_header(self) -> None:
        frame = ttk.Frame(self.root, padding=PAD)
        frame.pack(fill="x")
        big = font.nametofont("TkDefaultFont").copy()
        big.configure(size=HEADLINE_POINTS, weight="bold")
        self._maintenance = tkinter.StringVar()
        self._opmode = tkinter.StringVar()
        self._summary = tkinter.StringVar()
        self._degraded = tkinter.StringVar()
        for row, (label, var) in enumerate([("In maintenance:", self._maintenance), ("Opmode:", self._opmode)]):
            ttk.Label(frame, text=label, font=big).grid(row=row, column=0, sticky="w")
            ttk.Label(frame, textvariable=var, font=big).grid(row=row, column=1, sticky="w", padx=(PAD_WIDE, 0))
        ttk.Label(frame, textvariable=self._summary).grid(row=2, column=0, columnspan=2, sticky="w", pady=(PAD_INNER, 0))
        ttk.Label(frame, textvariable=self._degraded, foreground=self._colors[Severity.WARNING]).grid(
            row=3, column=0, columnspan=2, sticky="w"
        )

    def _build_table(self, title: str, columns: tuple[str, ...]) -> ttk.Treeview:
        """Its height follows its rows (:func:`_redraw`), so no row is ever below the fold."""
        frame = ttk.Labelframe(self.root, text=title, padding=PAD_INNER)
        frame.pack(fill="x", padx=PAD, pady=PAD_INNER)
        table = ttk.Treeview(frame, columns=columns, show="headings", height=1)
        char = font.nametofont("TkDefaultFont").measure("0")
        for column in columns:
            table.heading(column, text=column)
            width = char * COLUMN_CHARS.get(column, DEFAULT_COLUMN_CHARS)
            table.column(column, width=width, minwidth=width, stretch=column == "detail")
        for severity, color in self._colors.items():
            table.tag_configure(SEVERITY_STYLE[severity], foreground=color)
        table.pack(fill="x")
        return table

    def _build_log(self) -> None:
        frame = ttk.Labelframe(self.root, text="Log", padding=PAD_INNER)
        frame.pack(fill="both", expand=True, padx=PAD, pady=(PAD_INNER, PAD))
        controls = ttk.Frame(frame)
        controls.pack(fill="x")
        ttk.Label(controls, text="Level:").pack(side="left")
        level = initial_level(self._settings.log_level)
        if level != self._settings.log_level.upper():
            logger.warning(f"gui.log_level {self._settings.log_level!r} is not one of {LEVELS}; the window shows {level}")
        self._level = tkinter.StringVar(value=level)
        combo = ttk.Combobox(controls, textvariable=self._level, values=LEVELS, width=LEVEL_BOX_CHARS, state="readonly")
        combo.pack(side="left", padx=(PAD_INNER, PAD_WIDE))
        self._autoscroll = tkinter.BooleanVar(value=True)
        ttk.Checkbutton(controls, text="Follow the tail", variable=self._autoscroll).pack(side="left")
        mono = font.nametofont("TkFixedFont").copy()
        mono.configure(size=LOG_FONT_POINTS)
        if THEMED:
            import ttkbootstrap

            scrolled = ttkbootstrap.ScrolledText(frame, wrap="none", font=mono, state="disabled", height=LOG_PANE_LINES)
            scrolled.pack(fill="both", expand=True, pady=(PAD_INNER, 0))
            text: tkinter.Text = scrolled.text
        else:
            text = ScrolledText(frame, wrap="none", font=mono, state="disabled", height=LOG_PANE_LINES)
            text.pack(fill="both", expand=True, pady=(PAD_INNER, 0))
        for severity, color in self._colors.items():
            text.tag_configure(SEVERITY_STYLE[severity], foreground=color)
        text.tag_configure(SEVERITY_STYLE[Severity.OK], foreground="")  # ordinary lines in the theme's color
        self._pane = _TextPane(text, self._autoscroll)

    # --- running -----------------------------------------------------------------------------

    def run(self, stop: threading.Event, alive: Callable[[], bool]) -> None:
        """Show the window until ``stop`` is set or ``alive`` turns false, then close it."""
        self.root.after(0, self._watch_tick, stop, alive)
        self.root.after(0, self._drain_tick)
        self.root.after(0, self._refresh_tick)
        self.root.mainloop()
        self.root.destroy()

    def _watch_tick(self, stop: threading.Event, alive: Callable[[], bool]) -> None:
        """Nothing here can raise: Tk drops a callback that raises, and this one is the way out.

        It is also what lets Python run the SIGINT handler: Tk's mainloop is C, and signals are
        only handled between Python bytecodes.
        """
        if stop.is_set() or not alive():
            self.root.quit()
            return
        self.root.after(self._settings.drain_interval_ms, self._watch_tick, stop, alive)

    def _drain_tick(self) -> None:
        settings = self._settings
        try:
            level = logging.getLevelName(self._level.get())
            drain(self._lines, self._pane, settings.drain_max_records_per_tick, settings.log_pane_max_lines, level)
        finally:
            self.root.after(settings.drain_interval_ms, self._drain_tick)

    def _refresh_tick(self) -> None:
        try:
            snapshot = self._snapshot()
            top = header(snapshot, self._now())
            self._maintenance.set(top.maintenance)
            self._opmode.set(top.opmode)
            self._summary.set(top.summary)
            self._degraded.set(f"config degraded: {top.degraded}" if top.degraded else "")
            if snapshot is not self._shown:
                self._shown = snapshot
                _redraw(self._resources, resource_rows(snapshot), NOT_YET_PROBED)
                _redraw(self._processes, process_rows(snapshot), NO_PROCESSES_YET)
        finally:
            self.root.after(REFRESH_MS, self._refresh_tick)

    def _minimize(self) -> None:
        self.root.iconify()
        logger.info(CLOSE_HINT)

    # --- menu --------------------------------------------------------------------------------

    def _about(self) -> None:
        text = about_text(self._snapshot(), self._context.version, self._context.top, self._context.log_file)
        body = self._dialog(f"About {TITLE}")
        ttk.Label(body, text=text, justify="left").pack(anchor="w")
        ttk.Button(body, text="Copy diagnostics", command=lambda: self._copy(text)).pack(pady=(PAD_WIDE, 0))

    def _copy(self, text: str) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(text)

    def _help(self) -> None:
        log_file = self._context.log_file
        body = self._dialog(f"{TITLE} help")
        ttk.Label(body, text=help_text(self._context.readme, log_file), justify="left").pack(anchor="w")
        if log_file is not None and sys.platform == "win32":
            folder = log_file.parent
            ttk.Button(body, text="Open the log folder", command=lambda: os.startfile(folder)).pack(pady=(PAD_WIDE, 0))

    def _dialog(self, title: str) -> ttk.Frame:
        """A small window whose content sits on a themed frame; a bare Toplevel is not themed."""
        dialog = tkinter.Toplevel(self.root)
        dialog.title(title)
        body = ttk.Frame(dialog, padding=PAD_WIDE)
        body.pack(fill="both", expand=True)
        return body


def _redraw(table: ttk.Treeview, rows: list[TableRow], empty: str) -> None:
    table.delete(*table.get_children())
    table.configure(height=max(1, len(rows)))
    if not rows:
        table.insert("", "end", values=(empty,))
    for row in rows:
        table.insert("", "end", values=row.values, tags=(SEVERITY_STYLE[row.severity],))
