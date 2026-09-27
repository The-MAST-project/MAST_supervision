"""The window's log drain, kept free of tkinter so it is tested on Linux CI.

Three rules for everything the window shows:

- no worker thread touches a widget;
- everything crosses threads on the ``DequeHandler`` deque or on snapshots published by
  whole-reference assignment;
- tables are redrawn from a snapshot, never from live state.

``gui.py`` adapts a Tk ``ScrolledText`` to :class:`LogPane` and re-arms :func:`drain`
with ``root.after(drain_interval_ms, ...)``.

Design: mast-claude-config ``plans/supervisor-design.md`` §7, *The window*.
"""

from __future__ import annotations

from collections import deque
from typing import Protocol

from supervision.logsink import LogLine


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
