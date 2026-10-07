"""The log records the supervisor's window shows, handed across threads on a bounded deque.

:class:`DequeHandler` sits on the root logger beside the daily file handler, so any
thread's record reaches the window without touching a widget. The Tk thread drains it
(``gui_model.drain``); nothing else reads it.

No lock: ``deque.append`` and ``deque.popleft`` are atomic under the GIL, the same
reasoning MAST_common's ``notifications.py`` and ``stopping.py`` rely on.

Design: mast-claude-config ``plans/supervisor-design.md`` §7, *The window*.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class LogLine:
    levelno: int
    text: str


class DequeHandler(logging.Handler):
    """Keeps the newest ``maxlen`` formatted records; older ones fall off the far end.

    Subclasses bare ``logging.Handler`` and never ``logging.StreamHandler``: MAST_common's
    ``init_log`` adds its console handler only when the root logger has no
    ``StreamHandler``, so a subclass of one would silently remove the console from the
    whole process.

    Formats in ``emit``, on the producing thread, so the Tk thread only inserts text.
    """

    def __init__(self, maxlen: int, level: int = logging.NOTSET) -> None:
        super().__init__(level)
        self.lines: deque[LogLine] = deque(maxlen=maxlen)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.lines.append(LogLine(record.levelno, self.format(record)))
        except Exception:  # noqa: BLE001 -- logging's own contract: report via handleError, never raise
            self.handleError(record)
