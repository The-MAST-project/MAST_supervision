import logging
from collections import deque

from supervision.gui_model import drain
from supervision.logsink import LogLine


class FakeText:
    """Stands in for the Tk log pane: no tkinter, so the drain runs on Linux CI."""

    def __init__(self) -> None:
        self.lines: list[LogLine] = []

    def append_line(self, line: LogLine) -> None:
        self.lines.append(line)

    def line_count(self) -> int:
        return len(self.lines)

    def delete_oldest(self, count: int) -> None:
        del self.lines[:count]


def _lines(n: int, level: int = logging.INFO) -> deque[LogLine]:
    return deque(LogLine(level, f"m{i}") for i in range(n))


def test_takes_at_most_the_per_tick_cap_and_leaves_the_rest() -> None:
    lines, pane = _lines(250), FakeText()
    assert drain(lines, pane, 200, 2000, logging.DEBUG) == 200
    assert len(lines) == 50
    assert pane.lines[-1].text == "m199"


def test_trims_the_oldest_lines_from_the_pane() -> None:
    pane = FakeText()
    drain(_lines(40), pane, 100, 30, logging.DEBUG)
    assert [line.text for line in pane.lines] == [f"m{i}" for i in range(10, 40)]


def test_filtered_lines_are_drained_not_shown() -> None:
    lines = deque([LogLine(logging.DEBUG, "quiet"), LogLine(logging.ERROR, "loud")])
    pane = FakeText()
    assert drain(lines, pane, 200, 2000, logging.INFO) == 2
    assert not lines
    assert [line.text for line in pane.lines] == ["loud"]


def test_empty_deque_is_a_no_op() -> None:
    pane = FakeText()
    assert drain(deque(), pane, 200, 2000, logging.DEBUG) == 0
    assert pane.lines == []
