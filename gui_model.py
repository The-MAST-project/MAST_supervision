"""The window's log drain, kept free of tkinter so it is tested on Linux CI.

Three rules for everything the window shows:

- no worker thread touches a widget;
- everything crosses threads on the ``DequeHandler`` deque or on snapshots published by
  whole-reference assignment;
- tables are redrawn from a snapshot, never from live state.

``gui.py`` adapts a Tk ``ScrolledText`` to :class:`LogPane` and re-arms :func:`drain`
with ``root.after(drain_interval_ms, ...)``. What the header, the tables and the About box
say is decided here, from a snapshot, so ``gui.py`` only places text.

Design: mast-claude-config ``plans/supervisor-design.md`` §7, *The window*.
"""

from __future__ import annotations

import tomllib
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from supervision.heartbeat import format_uptime, or_unknown
from supervision.logsink import LogLine
from supervision.state import Severity, SupervisorSnapshot

LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
DEFAULT_LEVEL = "INFO"
CLOSE_HINT = "the window was minimized, not closed: the supervisor keeps running; stop it with Ctrl-C in its console"


class LogPane(Protocol):
    def append_line(self, line: LogLine) -> None: ...

    def line_count(self) -> int: ...

    def delete_oldest(self, count: int) -> None: ...


def drain(lines: deque[LogLine], pane: LogPane, max_per_tick: int, max_pane_lines: int, min_level: int) -> int:
    """Move up to ``max_per_tick`` lines into the pane; return how many were taken.

    The per-tick cap is what keeps a log storm from freezing the window: the rest wait for
    the next tick. Lines below ``min_level`` are taken and dropped, so a filtered-out
    storm still drains. The pane is then trimmed to its newest ``max_pane_lines``.

    The limits come from ``GuiConfig``; ``gui.py`` passes them, so this module does not
    import ``common.config``.
    """
    taken = 0
    while taken < max_per_tick and lines:
        line = lines.popleft()
        taken += 1
        if line.levelno >= min_level:
            pane.append_line(line)

    excess = pane.line_count() - max_pane_lines
    if excess > 0:
        pane.delete_oldest(excess)
    return taken


@dataclass(frozen=True, slots=True)
class Header:
    maintenance: str  # the two an operator acts on, shown large
    opmode: str
    summary: str  # hostname . role . phase . uptime . config generation
    degraded: str  # the configuration's degraded reason, verbatim, or ""


@dataclass(frozen=True, slots=True)
class TableRow:
    values: tuple[str, ...]
    severity: Severity


RESOURCE_COLUMNS = ("resource", "state", "detail", "since")
PROCESS_COLUMNS = ("program", "mode", "state", "pid", "restarts", "detail", "since")


def header(snapshot: SupervisorSnapshot, now: datetime) -> Header:
    """``now`` is the wall clock, so the uptime moves between publications."""
    summary = "  \u00b7  ".join(
        [
            snapshot.hostname,
            snapshot.role,
            snapshot.phase,
            f"up {format_uptime(now - snapshot.started_at)}",
            f"config gen {or_unknown(snapshot.config_generation)}",
        ]
    )
    return Header(
        maintenance=or_unknown(snapshot.in_maintenance),
        opmode=or_unknown(snapshot.opmode),
        summary=summary,
        degraded=snapshot.config_degraded_reason or "",
    )


def resource_rows(snapshot: SupervisorSnapshot) -> list[TableRow]:
    return [TableRow((r.name, r.state, r.detail, _clock(r.since)), r.state.severity) for r in snapshot.resources]


def process_rows(snapshot: SupervisorSnapshot) -> list[TableRow]:
    return [
        TableRow(
            (
                p.name,
                str(p.mode) if p.mode else "not supervised",
                p.state,
                or_unknown(p.pid),
                str(p.restarts),
                p.detail,
                _clock(p.since),
            ),
            p.state.severity,
        )
        for p in snapshot.processes
    ]


def about_text(snapshot: SupervisorSnapshot, version: str, top: Path, log_file: Path | None) -> str:
    """What About shows and its copy button puts on the clipboard: what a bug report needs."""
    return "\n".join(
        [
            f"mast-supervisor {version}",
            f"top: {top}",
            f"host: {snapshot.hostname}",
            f"role: {snapshot.role}",
            f"phase: {snapshot.phase}",
            f"opmode: {or_unknown(snapshot.opmode)}",
            f"config generation: {or_unknown(snapshot.config_generation)}",
            f"started: {snapshot.started_at:%Y-%m-%d %H:%M:%S}Z",
            f"log: {log_file or UNKNOWN_LOG}",
        ]
    )


UNKNOWN_LOG = "no daily log file"


def help_text(readme: Path, log_file: Path | None) -> str:
    return "\n".join([f"Documentation: {readme}", f"Logs: {log_file.parent if log_file else UNKNOWN_LOG}"])


def initial_level(configured: str) -> str:
    """The filter's starting level: the configured one when it names a filter level, else INFO."""
    return configured.upper() if configured.upper() in LEVELS else DEFAULT_LEVEL


def package_version() -> str:
    """The repo is imported through ``mast.pth`` and never installed, so pyproject.toml is the source."""
    pyproject = Path(__file__).with_name("pyproject.toml")
    return tomllib.loads(pyproject.read_text())["project"]["version"]


def _clock(when: datetime) -> str:
    return f"{when:%H:%M:%S}Z"
