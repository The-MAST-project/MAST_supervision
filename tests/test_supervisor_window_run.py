import logging
import signal
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from types import FrameType, SimpleNamespace
from typing import cast

import pytest

import supervision
from supervision.__main__ import open_gui, run_with_window
from supervision.logsink import DequeHandler
from supervision.supervisor import Supervisor

WAIT = 5.0
Loop = Callable[[threading.Event], None]


class FakeWindow:
    """Stands in for the Tk window: runs on the calling thread like the mainloop, until told."""

    def __init__(self, action: Callable[[threading.Event], None] | None = None) -> None:
        self.action = action
        self.ran_on: threading.Thread | None = None
        self.closed = False

    def run(self, stop: threading.Event, alive: Callable[[], bool]) -> None:
        self.ran_on = threading.current_thread()
        if self.action is not None:
            self.action(stop)
        deadline = threading.Event()
        timer = threading.Timer(WAIT, deadline.set)
        timer.start()
        while not stop.is_set() and alive() and not deadline.is_set():
            stop.wait(0.01)
        timer.cancel()
        assert not deadline.is_set(), "the window was never told to close"
        self.closed = True


def loop_until_stopped(seen: list[str]) -> Loop:
    def loop(stop: threading.Event) -> None:
        seen.append(threading.current_thread().name)
        stop.wait(WAIT)

    return loop


def test_the_window_runs_on_this_thread_and_the_loop_on_a_worker() -> None:
    seen: list[str] = []
    window = FakeWindow(action=lambda stop: stop.set())

    run_with_window(loop_until_stopped(seen), window, threading.Event())

    assert window.ran_on is threading.current_thread()
    assert seen == ["supervisor-loop"]


def test_ctrl_c_stops_both_and_the_handler_is_restored() -> None:
    before = signal.getsignal(signal.SIGINT)
    stop = threading.Event()

    def press_ctrl_c(_stop: threading.Event) -> None:
        handler = signal.getsignal(signal.SIGINT)
        assert callable(handler)
        handler(signal.SIGINT, cast(FrameType | None, None))

    window = FakeWindow(action=press_ctrl_c)
    run_with_window(loop_until_stopped([]), window, stop)

    assert stop.is_set() and window.closed
    assert signal.getsignal(signal.SIGINT) is before


def test_a_loop_that_ends_closes_the_window() -> None:
    window = FakeWindow()

    run_with_window(lambda stop: None, window, threading.Event())

    assert window.closed


def test_a_loop_that_fails_closes_the_window_and_its_error_is_raised() -> None:
    def failing(stop: threading.Event) -> None:
        raise RuntimeError("loop broke")

    window = FakeWindow()
    with pytest.raises(RuntimeError, match="loop broke"):
        run_with_window(failing, window, threading.Event())

    assert window.closed


def _deque_handlers() -> list[logging.Handler]:
    return [h for h in logging.getLogger().handlers if isinstance(h, DequeHandler)]


def test_a_python_without_tk_runs_headless(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    monkeypatch.setitem(sys.modules, "supervision.gui", None)  # import raises ImportError

    with caplog.at_level(logging.WARNING):
        window = open_gui(cast(Supervisor, None), Path("/top"))

    assert window is None
    assert "running headless" in caplog.text
    assert _deque_handlers() == []


def test_a_window_that_cannot_open_leaves_no_log_handler_behind(monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[object] = []

    def open_window(*args: object) -> None:
        opened.append(args)
        assert _deque_handlers(), "the handler feeds the window, so it is attached before it opens"

    fake_gui = SimpleNamespace(WindowSettings=lambda *a: a, WindowContext=lambda *a: a, open_window=open_window)
    monkeypatch.setitem(sys.modules, "supervision.gui", fake_gui)
    monkeypatch.setattr(supervision, "gui", fake_gui, raising=False)
    supervisor = SimpleNamespace(snapshot=None, wall_clock=lambda: None)

    window = open_gui(cast(Supervisor, supervisor), Path("/top"))

    assert window is None and opened
    assert _deque_handlers() == []
