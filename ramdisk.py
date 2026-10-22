"""Reviving the RAM disk (``D:``) when it is gone: on request, and on the supervisor's own probe.

``D:`` is an ImDisk RAM disk mounted at boot by a scheduled task, and Windows neither watches
nor restarts a scheduled task, so a disk that dies mid-run stays dead. A revival runs
``RESTART_TASK``, a SYSTEM task the imdisk provider registers: it force-detaches ``D:`` and runs
the boot task again, so the image stays named in one place and the indexes come back with it.
The supervisor is not elevated and cannot detach a disk itself; the task's ACL lets the
interactive user run it.

**Restarting wipes ``D:``**, so every rule here keeps it to when that is already true:

- **A request is a hint.** ``D:`` is probed with a write first, outside the lock; a writable disk
  answers ``healthy`` and nothing is done -- or ``restored`` shortly after a revival, so a caller
  who asked during it and asks again hears the outcome. A probe that a revival finished during is
  repeated: its "not writable" is older than the disk the revival brought back.
- **One revival at a time.** A request during a revival joins it and gets its outcome.
- **A limit.** ``GIVE_UP_FAILURES`` failed or short-lived revivals inside ``GIVE_UP_WINDOW_SECONDS``
  stop the revivals until the oldest one leaves the window; meanwhile every request answers
  ``giving_up`` and the snapshot carries it.

Design: mast-claude-config ``plans/filing-areas-design.md`` §8.1.
"""

from __future__ import annotations

import subprocess
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from common.mast_logging import get_logger
from supervision.clock import Clock
from supervision.resources import Probe
from supervision.state import RamdiskRevival, ResourceState, RevivalState

logger = get_logger(__name__)

RESTART_TASK = r"\mast-ramdisk-restart"
SCHTASKS_TIMEOUT_SECONDS = 30.0
WRITE_PROBE_SECONDS = 2.0
SUPERVISOR = "supervisor"
RECENT_SECONDS = 300.0


class Answer(StrEnum):
    HEALTHY = "healthy"  # D: is writable: nothing done
    RESTORING = "restoring"  # a revival runs; ask again for its outcome
    RESTORED = "restored"  # D: is back, indexes included
    FAILED = "failed"  # this revival did not bring D: back
    GIVING_UP = "giving_up"  # the limit is reached: no revival is attempted


@dataclass(frozen=True, slots=True)
class RevivalSettings:
    restore_budget_seconds: float = 300.0  # loading the 15 GB image into memory takes a while
    restore_poll_seconds: float = 2.0
    request_wait_seconds: float = 5.0  # a request waits this long for the outcome, then answers restoring
    short_lived_seconds: float = RECENT_SECONDS  # a disk lost again this soon after a revival counts it failed
    recent_restore_seconds: float = RECENT_SECONDS  # a writable disk this soon after a revival answers restored
    give_up_failures: int = 3
    give_up_window_seconds: float = 3600.0


DEFAULT_SETTINGS = RevivalSettings()

Runner = Callable[[Sequence[str]], int]  # argv -> exit code
WriteProbe = Callable[[], bool]  # True when D: takes a write


def run_command(argv: Sequence[str]) -> int:
    """Run ``argv`` to completion, bounded, logging what it printed."""
    try:
        done = subprocess.run(list(argv), capture_output=True, text=True, timeout=SCHTASKS_TIMEOUT_SECONDS, check=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        logger.error(f"{argv[0]}: {type(e).__name__}: {e}")
        return -1
    output = " ".join(f"{done.stdout} {done.stderr}".split())
    logger.info(f"{' '.join(argv)} -> exit {done.returncode}: {output}")
    return done.returncode


def restart_command() -> list[str]:
    return ["schtasks", "/run", "/tn", RESTART_TASK]


def probe_writable(root: str, timeout: float = WRITE_PROBE_SECONDS) -> bool:
    """True when a folder can be created and removed under ``root`` within ``timeout``."""
    outcome: list[bool] = []

    def _check() -> None:
        probe = Path(root) / f".supervisor-write-probe-{uuid.uuid4().hex}"
        try:
            probe.mkdir()
            probe.rmdir()
        except OSError:
            outcome.append(False)
            return
        outcome.append(True)

    thread = threading.Thread(target=_check, name="supervisor-ramdisk-write-probe", daemon=True)
    thread.start()
    thread.join(timeout)
    return bool(outcome) and outcome[0]


@dataclass(slots=True)
class _Revival:
    requested_by: str
    done: threading.Event = field(default_factory=threading.Event)
    answer: Answer = Answer.RESTORING


class RamdiskReviver:
    """Thread-safe: requests arrive on the API's threads, the loop's own on its thread."""

    def __init__(
        self,
        writable: WriteProbe,
        ready: Probe,
        runner: Runner,
        clock: Clock,
        settings: RevivalSettings = DEFAULT_SETTINGS,
        sleep: Callable[[float], None] = time.sleep,
        start: Callable[[Callable[[], None]], None] | None = None,
    ) -> None:
        self._writable = writable
        self._ready = ready
        self._runner = runner
        self._clock = clock
        self._settings = settings
        self._sleep = sleep
        self._start = start or _daemon_thread
        self._lock = threading.Lock()
        self._current: _Revival | None = None
        self._failures: deque[float] = deque()  # monotonic times of failed or short-lived revivals
        self._restored_monotonic: float | None = None
        self._lost_at: datetime | None = None
        self._restored_at: datetime | None = None
        self._requested_by: str | None = None
        self._detail = ""
        self._gave_up_logged = False
        self._generation = 0  # revivals finished

    def request(self, requested_by: str) -> Answer:
        """Revive ``D:`` if it is gone, and wait a little for the outcome."""
        revival = self._begin(requested_by)
        if isinstance(revival, Answer):
            return revival
        revival.done.wait(self._settings.request_wait_seconds)
        return revival.answer

    def nudge(self) -> None:
        """The supervisor's own probe found ``D:`` down: revive it without waiting for the outcome."""
        self._begin(SUPERVISOR)

    def status(self) -> RamdiskRevival | None:
        """None until ``D:`` has been lost once in this supervisor's life."""
        with self._lock:
            self._expire_failures()
            if self._lost_at is None and not self._failures:
                return None
            if self._current is not None:
                state = RevivalState.RESTORING
            elif self._giving_up():
                state = RevivalState.GIVING_UP
            else:
                state = RevivalState.IDLE
            return RamdiskRevival(
                state=state,
                lost_at=self._lost_at,
                restored_at=self._restored_at,
                requested_by=self._requested_by,
                failures_in_window=len(self._failures),
                detail=self._detail,
            )

    def _begin(self, requested_by: str) -> _Revival | Answer:
        while True:
            with self._lock:
                if self._current is not None:
                    return self._current
                generation = self._generation
            writable = self._writable()  # up to WRITE_PROBE_SECONDS on a wedged disk: never under the lock
            with self._lock:
                if self._current is not None:
                    return self._current
                if self._generation == generation:
                    break
        with self._lock:
            if writable:
                return Answer.RESTORED if self._recently_restored() else Answer.HEALTHY
            self._expire_failures()
            self._count_short_lived()
            self._note_loss(requested_by)
            if self._giving_up():
                self._log_giving_up()
                return Answer.GIVING_UP
            revival = _Revival(requested_by)
            self._current = revival
            self._detail = "restoring"
            self._gave_up_logged = False
        logger.warning(f"RAM disk lost; reviving it (requested by {requested_by})")
        self._start(lambda: self._revive(revival))
        return revival

    def _revive(self, revival: _Revival) -> None:
        try:
            answer, detail = self._run_and_wait()
        except Exception as e:
            logger.exception("RAM disk revival raised")
            answer, detail = Answer.FAILED, f"revival raised {type(e).__name__}: {e}"
        with self._lock:
            if answer is Answer.RESTORED:
                self._restored_at = self._clock.wall()
                self._restored_monotonic = self._clock.monotonic()
            else:
                self._failures.append(self._clock.monotonic())
                self._restored_monotonic = None
            self._detail = detail
            self._current = None
            self._generation += 1
            revival.answer = answer
            revival.done.set()
        if answer is Answer.RESTORED:
            logger.info(f"RAM disk restored: {detail}")
        else:
            logger.error(f"RAM disk not restored: {detail}")

    def _run_and_wait(self) -> tuple[Answer, str]:
        exit_code = self._runner(restart_command())
        if exit_code != 0:
            return Answer.FAILED, f"{RESTART_TASK} did not run (exit {exit_code})"
        deadline = self._clock.monotonic() + self._settings.restore_budget_seconds
        last = "not yet probed"
        while self._clock.monotonic() < deadline:
            result = self._ready()
            # Back is writable with its index folder reachable. The write tells the remounted disk
            # from the one that triggered the revival: schtasks returns before the task has
            # detached it, and a read-only D: passes the readiness probe. Missing indexes are the
            # image's content, which another restart cannot change; the resource check reports them.
            if result.state is not ResourceState.DOWN and self._writable():
                return Answer.RESTORED, result.detail
            last = result.detail
            self._sleep(self._settings.restore_poll_seconds)
        return Answer.FAILED, f"not back within {self._settings.restore_budget_seconds:g}s ({last})"

    def _recently_restored(self) -> bool:
        restored = self._restored_monotonic
        return restored is not None and self._clock.monotonic() - restored < self._settings.recent_restore_seconds

    def _note_loss(self, requested_by: str) -> None:
        """A new outage starts only when the disk was last known up: retries keep the first loss."""
        if self._lost_at is None or self._restored_at is not None:
            self._lost_at = self._clock.wall()
            self._restored_at = None
            self._requested_by = requested_by

    def _count_short_lived(self) -> None:
        """A disk lost again soon after its revival makes that revival a failure."""
        restored = self._restored_monotonic
        if restored is not None and self._clock.monotonic() - restored < self._settings.short_lived_seconds:
            self._failures.append(restored)
        self._restored_monotonic = None

    def _expire_failures(self) -> None:
        horizon = self._clock.monotonic() - self._settings.give_up_window_seconds
        while self._failures and self._failures[0] < horizon:
            self._failures.popleft()

    def _giving_up(self) -> bool:
        return len(self._failures) >= self._settings.give_up_failures

    def _log_giving_up(self) -> None:
        if not self._gave_up_logged:
            self._gave_up_logged = True
            self._detail = (
                f"giving up: {len(self._failures)} failed revivals within {self._settings.give_up_window_seconds:g}s"
            )
            logger.error(f"RAM disk: {self._detail}")


def _daemon_thread(work: Callable[[], None]) -> None:
    threading.Thread(target=work, name="supervisor-ramdisk-revival", daemon=True).start()
