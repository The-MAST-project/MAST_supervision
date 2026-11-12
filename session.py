"""The session bridge: how ``mast-service``, in session 0, starts the supervisor in the
interactive console session, holds it in a job, and asks it to stop.

``SessionBridge.spawn`` takes the console session's user token, makes it primary, builds the
user's environment from it, and creates the process **suspended** on ``winsta0\\default``,
assigns it to the job, and only then resumes it, so the supervisor never runs outside the job.
No console session, or nobody logged on to it, is not an error: nothing is spawned, the
caller is told when to retry (a doubling wait, 1 s to 60 s), and the change of state is
logged once.

The job (``KILL_ON_JOB_CLOSE | BREAKAWAY_OK``) is the backstop: closing it, or the service
dying, kills the supervisor, while what the supervisor starts with ``CREATE_BREAKAWAY_FROM_JOB``
survives. The stop event ``Global\\mast-supervisor-stop`` is the graceful path. Its DACL gives
SYSTEM and Administrators full access and interactive users ``SYNCHRONIZE`` only: the
supervisor, running as ``mast``, can wait on it but not signal it.

Win32 goes through pywin32, imported inside ``PyWin32SessionApi``'s methods so this module
imports on Linux CI, never through ``ctypes``, which the test spawn guard cannot see.

Design: mast-claude-config ``plans/supervisor-design.md`` §4.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any, Protocol

from supervision.backoff import Backoff

logger = logging.getLogger(__name__)

CONSOLE_DESKTOP = "winsta0\\default"
# WTSGetActiveConsoleSessionId's answer when no session is attached to the console, and
# the services' own session: neither has a desktop anyone sees.
NO_CONSOLE_SESSION = 0xFFFFFFFF
SERVICE_SESSION = 0
ERROR_NO_TOKEN = 1008

CREATE_SUSPENDED = 0x00000004
CREATE_UNICODE_ENVIRONMENT = 0x00000400
# Not the design's CREATE_NEW_CONSOLE: a console window's close button would kill the
# supervisor, which closing its window must never do.
CREATE_NO_WINDOW = 0x08000000
SPAWN_FLAGS = CREATE_SUSPENDED | CREATE_UNICODE_ENVIRONMENT | CREATE_NO_WINDOW
# What the supervisor adds to its own children under the service, so they outlive its job.
CREATE_BREAKAWAY_FROM_JOB = 0x01000000

JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0x00000800
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_LIMITS = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | JOB_OBJECT_LIMIT_BREAKAWAY_OK

STOP_EVENT_NAME = "Global\\mast-supervisor-stop"
STOP_EVENT_SDDL = "D:P(A;;GA;;;SY)(A;;GA;;;BA)(A;;0x00100000;;;IU)"
# How long one wait on the stop event lasts before the watcher looks at the loop's own stop again.
STOP_WATCH_POLL_MS = 1000

NO_SESSION_RETRY_FIRST_SECONDS = 1.0
NO_SESSION_RETRY_CAP_SECONDS = 60.0

ABANDONED_SPAWN_EXIT_CODE = 1
WAIT_OBJECT_0 = 0

Handle = Any


@dataclass(frozen=True, slots=True)
class Created:
    process: Handle
    thread: Handle
    pid: int


@dataclass(frozen=True, slots=True)
class Spawned:
    process: Handle
    pid: int


@dataclass(frozen=True, slots=True)
class Waiting:
    reason: str
    retry_after: float


class Win32SessionApi(Protocol):
    def active_console_session(self) -> int: ...

    def query_user_token(self, session_id: int) -> Handle | None:
        """The session's user token, or None when nobody is logged on to it."""
        ...

    def primary_token(self, token: Handle) -> Handle: ...

    def environment(self, token: Handle) -> dict[str, str]: ...

    def create_process(
        self,
        token: Handle,
        application: str,
        command_line: str,
        cwd: str,
        environment: dict[str, str],
        desktop: str,
        flags: int,
    ) -> Created: ...

    def assign_to_job(self, job: Handle, process: Handle) -> None: ...

    def resume(self, thread: Handle) -> None: ...

    def terminate(self, process: Handle) -> None: ...

    def create_job(self, limits: int) -> Handle: ...

    def create_event(self, name: str, sddl: str) -> Handle: ...


class StopEventError(Exception):
    """The stop event cannot be opened: something other than ``mast-service`` started a ``--service`` supervisor."""


class StopEventApi(Protocol):
    """The supervisor's side of the stop event: open it, and wait on it."""

    def open_event(self, name: str) -> Handle:
        """Raises :class:`StopEventError` when there is no such event."""
        ...

    def wait(self, event: Handle, timeout_ms: int) -> bool: ...


class SessionBridge:
    """Spawns into the console session, inside ``job``."""

    def __init__(self, api: Win32SessionApi, job: Handle) -> None:
        self._api = api
        self._job = job
        self._backoff = Backoff(NO_SESSION_RETRY_FIRST_SECONDS, NO_SESSION_RETRY_CAP_SECONDS)
        self._waiting_for: str | None = None

    def spawn(self, application: str, command_line: str, cwd: str) -> Spawned | Waiting:
        """``application`` is the executable's full path, so Windows never guesses it from the
        command line, whose first word is that path again."""
        session = self._api.active_console_session()
        if session in (NO_CONSOLE_SESSION, SERVICE_SESSION):
            return self._wait("no interactive console session")
        token = self._api.query_user_token(session)
        if token is None:
            return self._wait(f"nobody is logged on to console session {session}")
        primary = self._api.primary_token(token)
        created = self._api.create_process(
            primary, application, command_line, cwd, self._api.environment(primary), CONSOLE_DESKTOP, SPAWN_FLAGS
        )
        try:
            self._api.assign_to_job(self._job, created.process)
        except OSError:
            self._api.terminate(created.process)
            raise
        self._api.resume(created.thread)
        self._backoff.reset()
        self._waiting_for = None
        logger.info(f"session bridge: started pid {created.pid} in console session {session}")
        return Spawned(created.process, created.pid)

    def _wait(self, reason: str) -> Waiting:
        if reason != self._waiting_for:
            logger.info(f"session bridge: {reason}; waiting")
            self._waiting_for = reason
        return Waiting(reason, self._backoff.next())


def create_job(api: Win32SessionApi) -> Handle:
    return api.create_job(JOB_LIMITS)


def create_stop_event(api: Win32SessionApi, name: str = STOP_EVENT_NAME) -> Handle:
    return api.create_event(name, STOP_EVENT_SDDL)


def open_stop_event(api: StopEventApi, name: str = STOP_EVENT_NAME) -> Handle:
    return api.open_event(name)


def watch_stop_event(api: StopEventApi, event: Handle, stop: threading.Event) -> None:
    """Set ``stop`` when ``mast-service`` signals the event; return once ``stop`` is set either way."""
    while not stop.is_set():
        if api.wait(event, STOP_WATCH_POLL_MS):
            logger.info("supervisor: mast-service asked it to stop")
            stop.set()
            return


class PyWin32SessionApi:
    """``Win32SessionApi`` on the real kernel, through pywin32."""

    def active_console_session(self) -> int:
        import win32ts

        return win32ts.WTSGetActiveConsoleSessionId()

    def query_user_token(self, session_id: int) -> Handle | None:
        import pywintypes
        import win32ts

        try:
            return win32ts.WTSQueryUserToken(session_id)
        except pywintypes.error as e:
            if e.winerror == ERROR_NO_TOKEN:
                return None
            raise

    def primary_token(self, token: Handle) -> Handle:
        import win32security

        return win32security.DuplicateTokenEx(
            token, win32security.SecurityImpersonation, win32security.TOKEN_ALL_ACCESS, win32security.TokenPrimary
        )

    def environment(self, token: Handle) -> dict[str, str]:
        import win32profile

        return win32profile.CreateEnvironmentBlock(token, False)

    def create_process(
        self,
        token: Handle,
        application: str,
        command_line: str,
        cwd: str,
        environment: dict[str, str],
        desktop: str,
        flags: int,
    ) -> Created:
        import win32process
        import win32security

        startup = win32process.STARTUPINFO()
        startup.lpDesktop = desktop
        # Empty attributes are what None means to CreateProcessAsUser: default security, not inherited.
        process_attributes = win32security.SECURITY_ATTRIBUTES()
        thread_attributes = win32security.SECURITY_ATTRIBUTES()
        process, thread, pid, _ = win32process.CreateProcessAsUser(
            token, application, command_line, process_attributes, thread_attributes, False, flags, environment, cwd, startup
        )
        return Created(process, thread, pid)

    def assign_to_job(self, job: Handle, process: Handle) -> None:
        import win32job

        win32job.AssignProcessToJobObject(job, process)

    def resume(self, thread: Handle) -> None:
        import win32process

        win32process.ResumeThread(thread)

    def terminate(self, process: Handle) -> None:
        import win32process

        win32process.TerminateProcess(process, ABANDONED_SPAWN_EXIT_CODE)

    def create_job(self, limits: int) -> Handle:
        import win32job

        job: Handle = win32job.CreateJobObject(None, "")
        info = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
        info["BasicLimitInformation"]["LimitFlags"] = limits
        win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, info)
        return job

    def job_limits(self, job: Handle) -> int:
        import win32job

        info = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
        return info["BasicLimitInformation"]["LimitFlags"]

    def create_event(self, name: str, sddl: str) -> Handle:
        import win32event
        import win32security

        attributes = win32security.SECURITY_ATTRIBUTES()
        attributes.SECURITY_DESCRIPTOR = win32security.ConvertStringSecurityDescriptorToSecurityDescriptor(
            sddl, win32security.SDDL_REVISION_1
        )
        return win32event.CreateEvent(attributes, True, False, name)

    def open_event(self, name: str) -> Handle:
        import pywintypes
        import win32con
        import win32event

        try:
            return win32event.OpenEvent(win32con.SYNCHRONIZE, False, name)
        except pywintypes.error as e:
            raise StopEventError(
                f"cannot open {name} ({e.strerror}): --service is for mast-service, which creates it; "
                "run by hand with --no-service"
            ) from e

    def signal(self, event: Handle) -> None:
        import win32event

        win32event.SetEvent(event)

    def wait(self, event: Handle, timeout_ms: int) -> bool:
        import win32event

        return win32event.WaitForSingleObject(event, timeout_ms) == WAIT_OBJECT_0
