"""The session bridge: how session 0 starts a program in the interactive console session.

Against a fake ``Win32SessionApi`` on both CI legs; against the real kernel objects on
Windows only, for the two that spawn nothing (the job and the stop event).
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from typing import Any

import pytest

from supervision.backoff import Backoff
from supervision.session import (
    CONSOLE_DESKTOP,
    CREATE_NO_WINDOW,
    CREATE_SUSPENDED,
    CREATE_UNICODE_ENVIRONMENT,
    JOB_LIMITS,
    JOB_OBJECT_LIMIT_BREAKAWAY_OK,
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    NO_CONSOLE_SESSION,
    NO_SESSION_RETRY_CAP_SECONDS,
    NO_SESSION_RETRY_FIRST_SECONDS,
    SERVICE_SESSION,
    STOP_EVENT_NAME,
    STOP_EVENT_SDDL,
    Created,
    PyWin32SessionApi,
    SessionBridge,
    Spawned,
    Waiting,
    create_job,
    create_stop_event,
)

PYTHON = r"C:\MAST\.venv\Scripts\python.exe"
COMMAND = f'"{PYTHON}" -m supervision --service'
CWD = r"C:\MAST"
JOB = "job-handle"
CONSOLE = 1
ENVIRONMENT = {"USERNAME": "mast"}


@dataclass
class FakeApi:
    """Records each call in order; answers as a machine with ``mast`` logged on at the console."""

    session: int = CONSOLE
    logged_on: bool = True
    assign_fails: bool = False
    calls: list[tuple[Any, ...]] = field(default_factory=list)

    def active_console_session(self) -> int:
        self.calls.append(("active_console_session",))
        return self.session

    def query_user_token(self, session_id: int) -> object | None:
        self.calls.append(("query_user_token", session_id))
        return "user-token" if self.logged_on else None

    def primary_token(self, token: object) -> object:
        self.calls.append(("primary_token", token))
        return "primary-token"

    def environment(self, token: object) -> dict[str, str]:
        self.calls.append(("environment", token))
        return ENVIRONMENT

    def create_process(
        self,
        token: object,
        application: str,
        command_line: str,
        cwd: str,
        environment: dict[str, str],
        desktop: str,
        flags: int,
    ) -> Created:
        self.calls.append(("create_process", token, application, command_line, cwd, environment, desktop, flags))
        return Created(process="process-handle", thread="thread-handle", pid=4242)

    def assign_to_job(self, job: object, process: object) -> None:
        self.calls.append(("assign_to_job", job, process))
        if self.assign_fails:
            raise OSError("access denied")

    def resume(self, thread: object) -> None:
        self.calls.append(("resume", thread))

    def terminate(self, process: object) -> None:
        self.calls.append(("terminate", process))

    def create_job(self, limits: int) -> object:
        self.calls.append(("create_job", limits))
        return JOB

    def create_event(self, name: str, sddl: str) -> object:
        self.calls.append(("create_event", name, sddl))
        return "event-handle"

    def names(self) -> list[str]:
        return [call[0] for call in self.calls]


def _bridge(api: FakeApi) -> SessionBridge:
    return SessionBridge(api, JOB)


# --- no one to start it for -----------------------------------------------------------


@pytest.mark.parametrize("session", [NO_CONSOLE_SESSION, SERVICE_SESSION])
def test_no_console_session_spawns_nothing_and_waits(session: int) -> None:
    api = FakeApi(session=session)

    outcome = _bridge(api).spawn(PYTHON, COMMAND, CWD)

    assert isinstance(outcome, Waiting)
    assert outcome.retry_after == NO_SESSION_RETRY_FIRST_SECONDS
    assert "create_process" not in api.names()


def test_nobody_logged_on_spawns_nothing_and_waits() -> None:
    api = FakeApi(logged_on=False)

    outcome = _bridge(api).spawn(PYTHON, COMMAND, CWD)

    assert isinstance(outcome, Waiting)
    assert "create_process" not in api.names()


def test_waiting_backs_off_to_the_cap_and_a_spawn_resets_it() -> None:
    api = FakeApi(logged_on=False)
    bridge = _bridge(api)

    waits = [bridge.spawn(PYTHON, COMMAND, CWD) for _ in range(8)]
    assert [w.retry_after for w in waits if isinstance(w, Waiting)] == [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0]

    api.logged_on = True
    assert isinstance(bridge.spawn(PYTHON, COMMAND, CWD), Spawned)
    api.logged_on = False
    again = bridge.spawn(PYTHON, COMMAND, CWD)
    assert isinstance(again, Waiting)
    assert again.retry_after == NO_SESSION_RETRY_FIRST_SECONDS


def test_the_backoff_is_the_designs() -> None:
    assert (NO_SESSION_RETRY_FIRST_SECONDS, NO_SESSION_RETRY_CAP_SECONDS) == (1.0, 60.0)


def test_waiting_is_logged_once_per_state_change(caplog: pytest.LogCaptureFixture) -> None:
    api = FakeApi(logged_on=False)
    bridge = _bridge(api)

    with caplog.at_level("INFO", logger="supervision.session"):
        for _ in range(3):
            bridge.spawn(PYTHON, COMMAND, CWD)
        api.session = NO_CONSOLE_SESSION
        bridge.spawn(PYTHON, COMMAND, CWD)

    assert len(caplog.records) == 2


# --- the spawn ------------------------------------------------------------------------


def test_the_spawn_lands_on_the_interactive_desktop_with_a_unicode_environment() -> None:
    """Without ``winsta0\\default`` the process runs on the service's invisible window station
    and nothing errors; without CREATE_UNICODE_ENVIRONMENT the environment block is misread."""
    api = FakeApi()

    _bridge(api).spawn(PYTHON, COMMAND, CWD)

    (_, token, application, command, cwd, environment, desktop, flags) = next(
        c for c in api.calls if c[0] == "create_process"
    )
    assert desktop == CONSOLE_DESKTOP == "winsta0\\default"
    assert flags & CREATE_UNICODE_ENVIRONMENT
    assert (token, application, command, cwd, environment) == ("primary-token", PYTHON, COMMAND, CWD, ENVIRONMENT)


def test_the_spawn_is_suspended_and_windowless() -> None:
    api = FakeApi()

    _bridge(api).spawn(PYTHON, COMMAND, CWD)

    flags = next(c for c in api.calls if c[0] == "create_process")[-1]
    assert flags & CREATE_SUSPENDED
    assert flags & CREATE_NO_WINDOW


def test_the_process_joins_the_job_before_it_runs() -> None:
    api = FakeApi()

    outcome = _bridge(api).spawn(PYTHON, COMMAND, CWD)

    assert api.names()[-2:] == ["assign_to_job", "resume"]
    assert ("assign_to_job", JOB, "process-handle") in api.calls
    assert ("resume", "thread-handle") in api.calls
    assert outcome == Spawned(process="process-handle", pid=4242)


def test_the_environment_is_the_logged_on_users() -> None:
    api = FakeApi()

    _bridge(api).spawn(PYTHON, COMMAND, CWD)

    assert ("query_user_token", CONSOLE) in api.calls
    assert ("environment", "primary-token") in api.calls


def test_a_process_the_job_refuses_is_terminated_not_left_suspended() -> None:
    api = FakeApi(assign_fails=True)

    with pytest.raises(OSError, match="access denied"):
        _bridge(api).spawn(PYTHON, COMMAND, CWD)

    assert api.names()[-1] == "terminate"
    assert "resume" not in api.names()


# --- the job and the stop event -------------------------------------------------------


def test_the_job_kills_on_close_and_lets_children_break_away() -> None:
    api = FakeApi()

    assert create_job(api) == JOB
    assert api.calls == [("create_job", JOB_LIMITS)]
    assert JOB_LIMITS == JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | JOB_OBJECT_LIMIT_BREAKAWAY_OK


def test_the_stop_event_is_global_and_interactive_users_may_only_wait_on_it() -> None:
    api = FakeApi()

    create_stop_event(api)

    assert api.calls == [("create_event", STOP_EVENT_NAME, STOP_EVENT_SDDL)]
    assert STOP_EVENT_NAME == "Global\\mast-supervisor-stop"
    aces = STOP_EVENT_SDDL.removeprefix("D:P").strip("()").split(")(")
    assert "A;;GA;;;SY" in aces
    assert "A;;0x00100000;;;IU" in aces


# --- the real kernel objects, Windows only --------------------------------------------

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="the job and event APIs exist only on Windows")


@windows_only
def test_a_real_job_carries_both_limits() -> None:
    api = PyWin32SessionApi()

    job = create_job(api)

    assert api.job_limits(job) & JOB_LIMITS == JOB_LIMITS


@windows_only
def test_a_real_stop_event_is_signaled_and_waited_on() -> None:
    api = PyWin32SessionApi()
    event = create_stop_event(api, name=f"{STOP_EVENT_NAME}-test-{os.getpid()}")

    assert not api.wait(event, 0)
    api.signal(event)
    assert api.wait(event, 0)


def test_backoff_lives_outside_managed() -> None:
    """``service.py`` needs it and must not import ``managed.py``, which pulls in ``common.filer``."""
    assert Backoff.__module__ == "supervision.backoff"
