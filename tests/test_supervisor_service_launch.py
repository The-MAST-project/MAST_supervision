"""The service launch: ``--service`` beside ``--no-service``, the stop event, the spawn flags and the close hint.

``mast-service`` starts the supervisor with ``--service`` and asks it to stop through the stop
event; that stop tears the programs down. A developer's ``--no-service`` keeps today's behavior:
Ctrl-C leaves the programs running, and no child is spawned outside the terminal's job.
"""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

import pytest

import supervision.managed as managed_module
from supervision.__main__ import parse_args
from supervision.gui_model import CONSOLE_CLOSE_HINT, close_hint
from supervision.managed import OsProcessApi, spawn_flags
from supervision.session import (
    CREATE_BREAKAWAY_FROM_JOB,
    CREATE_NO_WINDOW,
    STOP_EVENT_NAME,
    PyWin32SessionApi,
    StopEventError,
    create_stop_event,
    open_stop_event,
    watch_stop_event,
)
from supervision.state import Launch
from supervision.supervisor import StopFlavor, stop_flavor

# --- the flag matrix ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("argv", "launch"),
    [
        (["--service"], Launch.SERVICE),
        (["--service", "--no-gui"], Launch.SERVICE),
        (["--service", "--no-app"], Launch.SERVICE),
        (["--no-service"], Launch.CONSOLE),
        (["--no-service", "--no-gui", "--no-app"], Launch.CONSOLE),
        (["--dry-run"], None),
    ],
)
def test_a_valid_command_line_names_its_launch(argv: list[str], launch: Launch | None) -> None:
    assert parse_args(argv).launch is launch


@pytest.mark.parametrize(
    ("argv", "says"),
    [
        ([], "--service"),
        (["--no-gui"], "--service"),
        (["--service", "--no-service"], "not allowed with argument"),
        (["--dry-run", "--service"], "--dry-run"),
        (["--dry-run", "--no-service"], "--dry-run"),
        (["--dry-run", "--no-gui"], "--dry-run"),
        (["--dry-run", "--no-app"], "--dry-run"),
    ],
)
def test_a_command_line_that_names_no_launch_or_two_is_refused(
    argv: list[str], says: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exited:
        parse_args(argv)

    assert exited.value.code == 2
    assert says in capsys.readouterr().err


# --- how each launch stops --------------------------------------------------------------


def test_the_service_tears_down_and_a_console_leaves_the_programs_running() -> None:
    assert stop_flavor(Launch.SERVICE) is StopFlavor.TEAR_DOWN
    assert stop_flavor(Launch.CONSOLE) is StopFlavor.LEAVE_RUNNING


class FakeEventApi:
    """The stop event as ``WaitForSingleObject`` sees it: signaled after ``waits_before`` timeouts."""

    def __init__(self, waits_before: int | None = None, missing: bool = False) -> None:
        self.waits_before = waits_before
        self.missing = missing
        self.waits: list[int] = []

    def open_event(self, name: str) -> object:
        if self.missing:
            raise StopEventError(f"{name} does not exist")
        return name

    def wait(self, event: object, timeout_ms: int) -> bool:
        self.waits.append(timeout_ms)
        return self.waits_before is not None and len(self.waits) > self.waits_before


def test_the_stop_event_sets_the_loop_stop() -> None:
    api = FakeEventApi(waits_before=2)
    stop = threading.Event()

    watch_stop_event(api, STOP_EVENT_NAME, stop)

    assert stop.is_set()
    assert len(api.waits) == 3


def test_a_loop_stopped_otherwise_ends_the_watch_without_waiting_forever() -> None:
    api = FakeEventApi()
    stop = threading.Event()
    stop.set()

    watch_stop_event(api, STOP_EVENT_NAME, stop)

    assert len(api.waits) <= 1
    assert all(timeout > 0 for timeout in api.waits)


def test_a_missing_stop_event_is_an_error_naming_the_event() -> None:
    with pytest.raises(StopEventError, match="mast-supervisor-stop"):
        open_stop_event(FakeEventApi(missing=True))


# --- the spawn flags --------------------------------------------------------------------


def test_only_the_service_launch_spawns_out_of_the_job() -> None:
    service = spawn_flags(Launch.SERVICE, "win32")
    console = spawn_flags(Launch.CONSOLE, "win32")

    assert service & CREATE_BREAKAWAY_FROM_JOB
    assert not console & CREATE_BREAKAWAY_FROM_JOB
    assert service & CREATE_NO_WINDOW and console & CREATE_NO_WINDOW


def test_no_creation_flags_off_windows() -> None:
    """``Popen`` refuses any ``creationflags`` but 0 outside Windows."""
    assert spawn_flags(Launch.SERVICE, "linux") == 0
    assert spawn_flags(Launch.CONSOLE, "linux") == 0


@pytest.mark.parametrize("launch", list(Launch))
def test_the_process_api_spawns_with_its_launchs_flags(launch: Launch, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    class Recorder:
        pid = 4242

        def __init__(self, argv: list[str], **kwargs: object) -> None:
            seen.update(kwargs)

    monkeypatch.setattr(managed_module.subprocess, "Popen", Recorder)

    OsProcessApi(launch, "win32").spawn(["ps3cli.exe"], Path("C:/ps3cli"))

    assert seen["creationflags"] == spawn_flags(launch, "win32")


# --- the close hint ---------------------------------------------------------------------


def test_the_service_window_says_how_to_stop_the_service() -> None:
    assert "sc stop mast-service" in close_hint(Launch.SERVICE)
    assert "Ctrl-C" not in close_hint(Launch.SERVICE)


def test_the_console_window_keeps_its_ctrl_c_hint() -> None:
    assert close_hint(Launch.CONSOLE) == CONSOLE_CLOSE_HINT
    assert "Ctrl-C" in CONSOLE_CLOSE_HINT


# --- the real event, Windows only ---------------------------------------------------------

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="the event APIs exist only on Windows")


@windows_only
def test_a_real_stop_event_is_opened_and_seen_signaled() -> None:
    api = PyWin32SessionApi()
    name = f"{STOP_EVENT_NAME}-open-test-{os.getpid()}"
    created = create_stop_event(api, name=name)

    opened = open_stop_event(api, name=name)
    api.signal(created)

    assert api.wait(opened, 0)


@windows_only
def test_a_real_missing_stop_event_is_a_stop_event_error() -> None:
    with pytest.raises(StopEventError):
        open_stop_event(PyWin32SessionApi(), name=f"{STOP_EVENT_NAME}-never-created-{os.getpid()}")
