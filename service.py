"""``mast-service``: the NSSM application that keeps the supervisor running in the console session.

NSSM runs ``python -m supervision.service`` from ``<top>`` as LocalSystem in session 0. This
module does four things and nothing else:

- takes the ``Global\\mast-service`` mutex, so a second copy (NSSM restarting it while an
  orphaned one still runs) exits instead of starting a second supervisor;
- starts ``python -m supervision --service`` in the console session through the session
  bridge, inside a kill-on-close job, and waits when nobody is logged on;
- watches the supervisor's process handle in one-second polls and restarts it on exit, after a
  wait doubling from 1 s to 60 s that starts again once a supervisor has run for 60 s;
- on stop, signals ``Global\\mast-supervisor-stop``, gives the supervisor
  :data:`SUPERVISOR_STOP_GRACE_SECONDS` to tear the programs down, and then closes the job,
  which kills a supervisor still there.

**NSSM stops it with a console Ctrl-C.** NSSM 2.24 tries a Ctrl-C on the application's
console, then ``WM_CLOSE`` to its windows, then ``WM_QUIT`` to its threads, then
``TerminateProcess``. A windowless Python has no window and no message loop, so only the first
reaches it. Ctrl-C sets the stop instead of raising ``KeyboardInterrupt``, which could land in
the middle of a Win32 call and skip the teardown; it is seen between polls, so no wait is
longer than :data:`POLL_MILLISECONDS`. The installer must give the console method more time
than the grace, skip the window and thread methods, and leave the process tree alone.

It never builds a ``Filer``, reads the configuration, probes ``Z:`` or ``D:``, or calls
``configure_logging()`` from MAST_common: session 0 cannot see the share or the RAM disk.
It logs with the standard library to ``C:\\MAST\\logs\\mast-service\\``.

Design: mast-claude-config ``plans/supervisor-design.md`` §3 and §4.
"""

from __future__ import annotations

import logging
import logging.handlers
import signal
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from supervision.backoff import Backoff
from supervision.session import (
    Handle,
    PyWin32SessionApi,
    SessionBridge,
    Spawned,
    Waiting,
    Win32SessionApi,
    create_job,
    create_stop_event,
)

logger = logging.getLogger(__name__)

SERVICE_MUTEX_NAME = "Global\\mast-service"
SERVICE_FLAG = "--service"
SUPERVISOR_MODULE = "supervision"
TOP = Path(__file__).resolve().parent.parent

POLL_MILLISECONDS = 1000
MILLISECONDS_PER_SECOND = 1000
RESTART_FIRST_SECONDS = 1.0
RESTART_CAP_SECONDS = 60.0
STABLE_RUN_SECONDS = 60.0

# What the supervisor's teardown can take, copied rather than imported: probes.py and
# supervisor.py import common.mast_logging, which builds a Filer. Each copy is pinned to its
# source by a test.
APP_SHUTDOWN_WAIT_SECONDS = 200.0  # probes.APP_SHUTDOWN_WAIT_SECONDS: the app's own shutdown
APP_STATUS_TIMEOUT_SECONDS = 70.0  # probes.APP_STATUS_TIMEOUT_SECONDS: a probe the loop waits for at exit
TEARDOWN_PROGRAMS = 4  # len(supervisor.TEARDOWN_ORDER)
PROGRAM_STOP_GRACE_SECONDS = 10.0  # SupervisorConfig.stop_grace_seconds' default: terminate, then kill
TEARDOWN_MARGIN_SECONDS = 30.0
SUPERVISOR_STOP_GRACE_SECONDS = (
    APP_SHUTDOWN_WAIT_SECONDS
    + TEARDOWN_PROGRAMS * 2 * PROGRAM_STOP_GRACE_SECONDS
    + APP_STATUS_TIMEOUT_SECONDS
    + TEARDOWN_MARGIN_SECONDS
)

ANOTHER_INSTANCE_EXIT_CODE = 2

LOG_FOLDER = Path(r"C:\MAST\logs\mast-service")
LOG_FILE = "mast-service.log"
LOG_MAX_BYTES = 10 * 1024 * 1024
LOG_BACKUPS = 5
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


class ServiceApi(Win32SessionApi, Protocol):
    def create_mutex(self, name: str) -> Handle | None:
        """The named mutex, owned; None when another process already holds it."""
        ...

    def wait(self, handle: Handle, timeout_ms: int) -> bool:
        """True once ``handle`` is signaled (a process: it has exited)."""
        ...

    def signal(self, event: Handle) -> None: ...

    def close(self, handle: Handle) -> None: ...


class Service:
    def __init__(
        self,
        api: ServiceApi,
        stop: threading.Event,
        application: str,
        cwd: str,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._api = api
        self._stop = stop
        self._application = application
        self._command_line = f'"{application}" -m {SUPERVISOR_MODULE} {SERVICE_FLAG}'
        self._cwd = cwd
        self._sleep = sleep
        self._monotonic = monotonic
        self._restarts = Backoff(RESTART_FIRST_SECONDS, RESTART_CAP_SECONDS)
        self._child: Spawned | None = None

    def run(self) -> int:
        mutex = self._api.create_mutex(SERVICE_MUTEX_NAME)
        if mutex is None:
            logger.error(f"another mast-service holds {SERVICE_MUTEX_NAME}; exiting without starting a supervisor")
            return ANOTHER_INSTANCE_EXIT_CODE
        job = create_job(self._api)
        event = create_stop_event(self._api)
        logger.info(f"mast-service started; supervisor command: {self._command_line}")
        try:
            self._watch(SessionBridge(self._api, job))
        finally:
            self._stop_supervisor(event, job)
            self._api.close(mutex)
        return 0

    def _watch(self, bridge: SessionBridge) -> None:
        started_at = 0.0
        while not self._stop.is_set():
            if self._child is None:
                match bridge.spawn(self._application, self._command_line, self._cwd):
                    case Waiting(retry_after=retry_after):
                        self._pause(retry_after)
                        continue
                    case Spawned() as spawned:
                        self._child = spawned
                        started_at = self._monotonic()
            if self._api.wait(self._child.process, POLL_MILLISECONDS):
                ran = self._monotonic() - started_at
                self._api.close(self._child.process)
                logger.warning(f"supervisor pid {self._child.pid} exited after {ran:.0f}s")
                self._child = None
                if ran >= STABLE_RUN_SECONDS:
                    self._restarts.reset()
                delay = self._restarts.next()
                logger.info(f"restarting the supervisor in {delay:g}s")
                self._pause(delay)

    def _pause(self, seconds: float) -> None:
        """Sleep in poll-sized slices, so a stop is never kept waiting for a whole retry."""
        deadline = self._monotonic() + seconds
        while not self._stop.is_set() and (remaining := deadline - self._monotonic()) > 0:
            self._sleep(min(remaining, POLL_MILLISECONDS / MILLISECONDS_PER_SECOND))

    def _stop_supervisor(self, event: Handle, job: Handle) -> None:
        logger.info("mast-service stopping: signaling the supervisor to tear down")
        self._api.signal(event)
        if self._child is not None:
            grace_ms = int(SUPERVISOR_STOP_GRACE_SECONDS * MILLISECONDS_PER_SECOND)
            if self._api.wait(self._child.process, grace_ms):
                logger.info(f"supervisor pid {self._child.pid} exited")
            else:
                logger.warning(
                    f"supervisor pid {self._child.pid} still running after {SUPERVISOR_STOP_GRACE_SECONDS:g}s; "
                    "closing the job kills it"
                )
        self._api.close(job)
        logger.info("mast-service stopped")


def install_stop_signals(stop: threading.Event) -> None:
    def on_signal(signum: int, frame: Any) -> None:
        logger.info(f"mast-service: signal {signum}; stopping")
        stop.set()

    signal.signal(signal.SIGINT, on_signal)
    sigbreak = getattr(signal, "SIGBREAK", None)
    if sigbreak is not None:
        signal.signal(sigbreak, on_signal)


def configure_logging(folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        folder / LOG_FILE, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)


def main() -> int:
    configure_logging(LOG_FOLDER)
    stop = threading.Event()
    install_stop_signals(stop)
    return Service(PyWin32SessionApi(), stop, application=sys.executable, cwd=str(TOP)).run()


if __name__ == "__main__":
    sys.exit(main())
