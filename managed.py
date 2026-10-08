"""One program the supervisor owns or watches: find it, adopt or spawn it, probe it, restart it.

**Adopt, never kill.** A copy already running in the supervisor's Windows login session is
adopted as-is, healthy or not; killing a PWI4 that holds a connected, tracking mount because a
supervisor restarted is destructive. A copy running in **another** login session (session 0,
or someone's Remote Desktop) is neither adopted nor killed, and no second copy is started
beside it: the row is ``blocked``, at ERROR, until that copy exits.

**Nothing here blocks.** :meth:`ManagedProcess.tick` is called by the supervisor loop. A probe
runs on the program's own single-thread executor and is never started while the previous one
is still running, so a slow probe delays only its own program. A restart is terminate, then
kill once the grace has passed, then respawn after a backoff -- each step taken on a later tick.

**A spawn gets a startup grace.** Failed probes do not count toward the threshold until the
program has probed healthy once or :data:`STARTUP_GRACE_SECONDS` has passed, so a slow cold
start is ``starting``, not a restart loop. An adopted program was already running and gets none.

The operating system is reached only through :class:`ProcessApi`, so every rule here is tested
against a fake; :class:`OsProcessApi` is the Windows implementation.

Design: mast-claude-config ``plans/supervisor-design.md`` §6.
"""

from __future__ import annotations

import os
import subprocess
from collections import deque
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import psutil

from common.mast_logging import get_logger
from supervision.clock import Clock
from supervision.probes import HealthResult
from supervision.state import ProcessState, ProcessStatus, SupervisionMode

logger = get_logger(__name__)

STARTUP_GRACE_SECONDS = 120.0
# A tick follows phase changes it causes (found -> probed, stopped -> scheduled) rather than
# leaving each for the next tick; the longest chain is absent -> running -> stopping -> absent.
MAX_STEPS_PER_TICK = 4


@dataclass(frozen=True, slots=True)
class FoundProcess:
    pid: int
    session: int | None  # None when it cannot be read, typically another user's process
    started: float  # creation time, seconds since the epoch


class ProcessApi(Protocol):
    def find(self, image: str) -> list[FoundProcess]: ...

    def current_session(self) -> int: ...

    def spawn(self, argv: list[str], cwd: Path) -> int: ...

    def is_running(self, pid: int) -> bool: ...

    def exit_code(self, pid: int) -> int | None: ...  # None for a process we did not spawn

    def terminate(self, pid: int) -> None: ...

    def kill(self, pid: int) -> None: ...


@dataclass(frozen=True, slots=True)
class RestartPolicy:
    """The fields of ``common.config.supervisor.ManagedProcessConfig`` this module reads, plus the
    supervisor's ``stop_grace_seconds``. A local type, so nothing here imports ``common.config``.
    """

    probe_interval_seconds: float = 15.0
    unhealthy_threshold: int = 3
    restart_backoff_seconds: float = 5.0
    restart_backoff_cap_seconds: float = 120.0
    crash_loop_restarts: int = 5
    crash_loop_window_seconds: float = 600.0
    stop_grace_seconds: float = 10.0


class Backoff:
    """Doubling waits up to a cap. A copy of MAST_common's ``config._watcher._Backoff`` (MAST_supervision#2)."""

    def __init__(self, first: float, cap: float) -> None:
        self._first = first
        self._cap = cap
        self._next = first

    def reset(self) -> None:
        self._next = self._first

    def next(self) -> float:
        value = self._next
        self._next = min(self._next * 2, self._cap)
        return value


Submit = Callable[[Callable[[], HealthResult]], "Future[HealthResult]"]


@dataclass(slots=True)
class _Stopping:
    pid: int
    kill_at: float
    reason: str
    killed: bool = False


@dataclass(slots=True)
class _Running:
    pid: int
    spawned_at: float | None  # None when adopted: no startup grace
    probed_healthy: bool = False
    failures: int = 0
    probe: Future[HealthResult] | None = None
    next_probe_at: float = 0.0
    unstoppable: bool = False  # a stop was refused (e.g. an elevated copy); not attempted again


@dataclass(slots=True)
class _Absent:
    next_look_at: float = 0.0
    respawn_at: float | None = None


class ManagedProcess:
    def __init__(
        self,
        name: str,
        argv: list[str],
        mode: SupervisionMode,
        policy: RestartPolicy,
        probe: Callable[[], HealthResult],
        api: ProcessApi,
        clock: Clock,
        submit: Submit | None = None,
    ) -> None:
        if mode is SupervisionMode.DISABLED:
            raise ValueError(f"{name}: a disabled program is not managed")
        self.name = name
        self._image = Path(argv[0]).name
        self._argv = argv
        self._mode = mode
        self._policy = policy
        self._probe = probe
        self._api = api
        self._clock = clock
        self._submit = submit or ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"probe-{name}").submit
        self._backoff = Backoff(policy.restart_backoff_seconds, policy.restart_backoff_cap_seconds)
        self._restart_times: deque[float] = deque()
        self._phase: _Absent | _Running | _Stopping = _Absent()
        self._state = ProcessState.STARTING
        self._detail = "not yet looked for"
        self._since = clock.wall()
        self._restarts = 0
        self._last_exit_code: int | None = None
        # Other copies seen when the running one was adopted, kept on the row while it runs.
        self._others = ""

    @property
    def supervised(self) -> bool:
        return self._mode is SupervisionMode.SUPERVISED

    def status(self) -> ProcessStatus:
        pid = self._phase.pid if isinstance(self._phase, _Running | _Stopping) else None
        return ProcessStatus(
            name=self.name,
            mode=self._mode,
            state=self._state,
            pid=pid,
            restarts=self._restarts,
            last_exit_code=self._last_exit_code,
            detail=self._detail,
            since=self._since,
        )

    def tick(self) -> ProcessStatus:
        now = self._clock.monotonic()
        for _ in range(MAX_STEPS_PER_TICK):
            phase = self._phase
            match phase:
                case _Absent():
                    self._tick_absent(phase, now)
                case _Running():
                    self._tick_running(phase, now)
                case _Stopping():
                    self._tick_stopping(phase, now)
            if self._phase is phase:
                break
        return self.status()

    def retry(self) -> None:
        """Leave ``crash_looped``: forget the restart history and look for the program again."""
        if self._state is not ProcessState.CRASH_LOOPED:
            return
        self._restart_times.clear()
        self._backoff.reset()
        self._phase = _Absent()
        self._set(ProcessState.STARTING, "retrying")

    # --- phases ------------------------------------------------------------------------------

    def _tick_absent(self, absent: _Absent, now: float) -> None:
        if self._state is ProcessState.CRASH_LOOPED:
            return
        if absent.respawn_at is not None and now < absent.respawn_at:
            return
        if absent.respawn_at is not None or now >= absent.next_look_at:
            absent.next_look_at = now + self._policy.probe_interval_seconds
            self._look(now)

    def _found(self) -> tuple[list[FoundProcess], list[FoundProcess]]:
        """The copies running in our login session, oldest first, and those running in another."""
        found = self._api.find(self._image)
        session = self._api.current_session()
        mine = sorted((p for p in found if p.session == session), key=lambda p: p.started)
        return mine, [p for p in found if p.session != session]

    @staticmethod
    def _foreign(foreign: list[FoundProcess]) -> str:
        return ", ".join(f"pid {p.pid} in session {p.session if p.session is not None else 'unknown'}" for p in foreign)

    def _describe_others(self, pid: int) -> str:
        mine, foreign = self._found()
        notes = []
        extras = [p for p in mine if p.pid != pid]
        if extras:
            notes.append(f"also running here: {', '.join(f'pid {p.pid}' for p in extras)}")
        if foreign:
            notes.append(f"also running in another login session: {self._foreign(foreign)}")
        return "; ".join(notes)

    def _look(self, now: float) -> None:
        mine, foreign = self._found()
        if mine:
            adopted = mine[0]
            self._phase = _Running(pid=adopted.pid, spawned_at=None)
            self._others = self._describe_others(adopted.pid)
            self._set(ProcessState.STARTING, f"adopted pid {adopted.pid}")
        elif foreign:
            detail = f"running in another login session ({self._foreign(foreign)}): not adopted, not killed"
            self._set(ProcessState.BLOCKED, detail)
        elif self.supervised:
            self._spawn(now)
        else:
            self._set(ProcessState.UNHEALTHY, "not running (observed: not started by the supervisor)")

    def _spawn(self, now: float) -> None:
        try:
            pid = self._api.spawn(self._argv, cwd=Path(self._argv[0]).parent)
        except OSError as e:
            self._schedule_restart(now, f"spawn failed: {e}")
            return
        logger.info(f"{self.name}: spawned pid {pid}: {self._argv}")
        self._others = ""
        self._phase = _Running(pid=pid, spawned_at=now)
        self._set(ProcessState.STARTING, f"spawned pid {pid}")

    def _tick_running(self, running: _Running, now: float) -> None:
        if not self._api.is_running(running.pid):
            self._on_exit(running, now)
            return
        if running.probe is None and now >= running.next_probe_at:
            running.next_probe_at = now + self._policy.probe_interval_seconds
            self._others = self._describe_others(running.pid)
            running.probe = self._submit(self._probe)
        if running.probe is not None and running.probe.done():
            result = self._result(running.probe)
            running.probe = None
            self._apply(running, result, now)

    def _result(self, probe: Future[HealthResult]) -> HealthResult:
        try:
            return probe.result()
        except Exception as e:  # noqa: BLE001 -- a failing probe is a failed check, never a dead supervisor
            logger.debug(f"{self.name}: probe raised", exc_info=True)
            return HealthResult(False, f"probe raised {type(e).__name__}: {e}")

    def _apply(self, running: _Running, result: HealthResult, now: float) -> None:
        if result.healthy:
            running.probed_healthy = True
            running.failures = 0
            self._backoff.reset()
            self._set(ProcessState.HEALTHY, result.detail)
            return
        in_grace = (
            running.spawned_at is not None
            and not running.probed_healthy
            and now - running.spawned_at < STARTUP_GRACE_SECONDS
        )
        if in_grace:
            self._set(ProcessState.STARTING, f"starting: {result.detail}")
            return
        running.failures += 1
        if running.unstoppable:
            self._set(ProcessState.BLOCKED, f"{result.detail}; pid {running.pid} cannot be stopped")
            return
        threshold = self._policy.unhealthy_threshold
        count = f"{running.failures}/{threshold}" if self.supervised else f"{running.failures} in a row"
        self._set(ProcessState.UNHEALTHY, f"{result.detail} ({count})")
        if self.supervised and running.failures >= threshold:
            self._stop(running, now, f"unhealthy: {result.detail}")

    def _on_exit(self, running: _Running, now: float) -> None:
        code = self._api.exit_code(running.pid)
        self._last_exit_code = code
        reason = f"pid {running.pid} exited" + (f" with code {code}" if code is not None else "")
        if self.supervised:
            self._schedule_restart(now, reason)
        else:
            self._phase = _Absent()
            self._set(ProcessState.UNHEALTHY, reason)

    def _stop(self, running: _Running, now: float, reason: str) -> None:
        logger.warning(f"{self.name}: terminating pid {running.pid} ({reason})")
        try:
            self._api.terminate(running.pid)
        except OSError as e:
            running.unstoppable = True
            self._set(ProcessState.BLOCKED, f"{reason}; cannot stop pid {running.pid}: {e}")
            return
        self._phase = _Stopping(pid=running.pid, kill_at=now + self._policy.stop_grace_seconds, reason=reason)
        self._set(ProcessState.UNHEALTHY, f"{reason}; stopping pid {running.pid}")

    def _tick_stopping(self, stopping: _Stopping, now: float) -> None:
        if self._api.is_running(stopping.pid):
            if now < stopping.kill_at:
                return
            if stopping.killed:
                self._set(ProcessState.BLOCKED, f"{stopping.reason}; pid {stopping.pid} survived kill")
                return
            logger.warning(f"{self.name}: killing pid {stopping.pid}, still running after the grace")
            stopping.killed = True
            stopping.kill_at = now + self._policy.stop_grace_seconds
            try:
                self._api.kill(stopping.pid)
            except OSError as e:
                self._set(ProcessState.BLOCKED, f"{stopping.reason}; cannot kill pid {stopping.pid}: {e}")
                return
            if self._api.is_running(stopping.pid):
                return
        self._last_exit_code = self._api.exit_code(stopping.pid)
        self._schedule_restart(now, stopping.reason)

    def _schedule_restart(self, now: float, reason: str) -> None:
        window = self._policy.crash_loop_window_seconds
        self._restart_times.append(now)
        while self._restart_times and now - self._restart_times[0] > window:
            self._restart_times.popleft()
        self._phase = _Absent()
        if len(self._restart_times) > self._policy.crash_loop_restarts:
            code = f", last exit code {self._last_exit_code}" if self._last_exit_code is not None else ""
            self._set(
                ProcessState.CRASH_LOOPED,
                f"{reason}; {len(self._restart_times)} exits in {window:g}s{code}; supervision stopped",
            )
            return
        delay = self._backoff.next()
        self._restarts += 1
        self._phase = _Absent(respawn_at=now + delay)
        logger.warning(f"{self.name}: {reason}; restarting in {delay:g}s")
        self._set(ProcessState.UNHEALTHY, f"{reason}; restarting in {delay:g}s")

    def _set(self, state: ProcessState, detail: str) -> None:
        if not isinstance(self._phase, _Running):
            self._others = ""
        if self._others:
            detail = f"{detail}; {self._others}"
        if state is not self._state:
            self._since = self._clock.wall()
            logger.log(state.severity, f"{self.name}: {state} ({detail})")
        self._state = state
        self._detail = detail


class OsProcessApi:
    """:class:`ProcessApi` over psutil and ``common.process``. Windows only: login sessions are.

    Our own children are held as ``Popen`` handles, which pin the pid and give the exit code;
    adopted processes as ``psutil.Process``, which checks the creation time against pid reuse.
    """

    def __init__(self) -> None:
        self._children: dict[int, subprocess.Popen[bytes]] = {}
        self._adopted: dict[int, psutil.Process] = {}

    def find(self, image: str) -> list[FoundProcess]:
        """By image name, ignoring case as Windows does."""
        from common.process import process_session_id

        wanted = image.casefold()
        found = []
        for proc in psutil.process_iter(["name", "create_time"]):
            if (proc.info["name"] or "").casefold() != wanted:
                continue
            try:
                session: int | None = process_session_id(proc.pid)
            except OSError:
                session = None
            if proc.pid not in self._children:
                self._adopted[proc.pid] = proc
            found.append(FoundProcess(pid=proc.pid, session=session, started=proc.info["create_time"] or 0.0))
        return found

    def current_session(self) -> int:
        from common.process import process_session_id

        return process_session_id(os.getpid())

    def spawn(self, argv: list[str], cwd: Path) -> int:
        child = subprocess.Popen(argv, cwd=cwd, shell=False)
        self._children[child.pid] = child
        return child.pid

    def is_running(self, pid: int) -> bool:
        if pid in self._children:
            return self._children[pid].poll() is None
        proc = self._adopted.get(pid)
        return proc is not None and proc.is_running()

    def exit_code(self, pid: int) -> int | None:
        child = self._children.get(pid)
        return child.returncode if child is not None else None

    def terminate(self, pid: int) -> None:
        self._signal(pid, kill=False)

    def kill(self, pid: int) -> None:
        self._signal(pid, kill=True)

    def _signal(self, pid: int, kill: bool) -> None:
        if pid in self._children:
            child = self._children[pid]
            if kill:
                child.kill()
            else:
                child.terminate()
            return
        proc = self._adopted.get(pid)
        if proc is None:
            return
        try:
            if kill:
                proc.kill()
            else:
                proc.terminate()
        except psutil.NoSuchProcess:
            return
        except psutil.AccessDenied as e:
            raise PermissionError(f"pid {pid}: access denied") from e
