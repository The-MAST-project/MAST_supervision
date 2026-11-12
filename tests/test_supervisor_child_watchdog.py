"""``mast-service``: holding the supervisor in the console session, restarting it, and stopping it.

Against a fake ``ServiceApi`` on both CI legs: nothing here spawns, waits on a real handle or
opens a real mutex.
"""

from __future__ import annotations

import logging
import logging.handlers
import signal
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from supervision import service
from supervision.service import (
    ANOTHER_INSTANCE_EXIT_CODE,
    POLL_MILLISECONDS,
    RESTART_CAP_SECONDS,
    RESTART_FIRST_SECONDS,
    SERVICE_FLAG,
    SERVICE_MUTEX_NAME,
    STABLE_RUN_SECONDS,
    SUPERVISOR_STOP_GRACE_SECONDS,
    Service,
    configure_logging,
    install_stop_signals,
)
from supervision.session import NO_CONSOLE_SESSION, NO_SESSION_RETRY_FIRST_SECONDS, STOP_EVENT_NAME, Created

PYTHON = r"C:\MAST\.venv\Scripts\python.exe"
TOP = r"C:\MAST"
CONSOLE = 1


@dataclass
class FakeClock:
    now: float = 0.0
    slept: list[float] = field(default_factory=list)

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


@dataclass
class FakeApi:
    """A console session with ``mast`` logged on. ``lives`` says, per spawn, how many
    ``POLL_MILLISECONDS`` polls the supervisor survives; past the list it never exits.
    ``stop`` is set after ``polls_before_stop`` polls, which is how a test ends the watch."""

    clock: FakeClock
    stop: threading.Event
    lives: list[int] = field(default_factory=list)
    polls_before_stop: int = 1_000
    session: int = CONSOLE
    mutex_taken: bool = False
    exits_on_stop_event: bool = True
    calls: list[tuple[Any, ...]] = field(default_factory=list)
    slept_at_spawn: list[float] = field(default_factory=list)
    _spawned: int = 0
    _polls_left: int = 0
    _total_polls: int = 0
    _signaled: bool = False

    # -- the session bridge's calls
    def active_console_session(self) -> int:
        return self.session

    def query_user_token(self, session_id: int) -> object:
        return "token"

    def primary_token(self, token: object) -> object:
        return "primary"

    def environment(self, token: object) -> dict[str, str]:
        return {}

    def create_process(self, token, application, command_line, cwd, environment, desktop, flags) -> Created:  # noqa: ANN001
        self._spawned += 1
        self.slept_at_spawn.append(sum(self.clock.slept))
        self._polls_left = self.lives[self._spawned - 1] if self._spawned <= len(self.lives) else -1
        self.calls.append(("spawn", application, command_line, cwd))
        return Created(process=f"process-{self._spawned}", thread=f"thread-{self._spawned}", pid=1000 + self._spawned)

    def assign_to_job(self, job: object, process: object) -> None:
        self.calls.append(("assign_to_job", job, process))

    def resume(self, thread: object) -> None:
        pass

    def terminate(self, process: object) -> None:
        self.calls.append(("terminate", process))

    def create_job(self, limits: int) -> object:
        self.calls.append(("create_job",))
        return "job"

    def create_event(self, name: str, sddl: str) -> object:
        self.calls.append(("create_event", name))
        return "stop-event"

    # -- the service's own calls
    def create_mutex(self, name: str) -> object | None:
        self.calls.append(("create_mutex", name))
        return None if self.mutex_taken else "mutex"

    def wait(self, handle: object, timeout_ms: int) -> bool:
        """A process handle: True once it has exited."""
        self.calls.append(("wait", handle, timeout_ms))
        if self._signaled:
            return self.exits_on_stop_event
        self._total_polls += 1
        if self._total_polls >= self.polls_before_stop:
            self.stop.set()
        self.clock.now += timeout_ms / 1000
        if self._polls_left == 0:
            return True
        self._polls_left -= 1
        return False

    def signal(self, event: object) -> None:
        self.calls.append(("signal", event))
        self._signaled = True

    def close(self, handle: object) -> None:
        self.calls.append(("close", handle))

    def names(self) -> list[str]:
        return [c[0] for c in self.calls]

    def restart_waits(self) -> list[float]:
        """The time slept between one spawn and the next."""
        return [b - a for a, b in zip(self.slept_at_spawn, self.slept_at_spawn[1:], strict=False)]


def _service(api: FakeApi) -> Service:
    return Service(api, api.stop, application=PYTHON, cwd=TOP, sleep=api.clock.sleep, monotonic=api.clock.monotonic)


def _api(**kwargs: Any) -> FakeApi:
    return FakeApi(clock=FakeClock(), stop=threading.Event(), **kwargs)


def test_it_starts_the_supervisor_with_the_service_flag_from_the_top_folder() -> None:
    api = _api(polls_before_stop=1)

    assert _service(api).run() == 0

    spawns = [c for c in api.calls if c[0] == "spawn"]
    assert spawns == [("spawn", PYTHON, f'"{PYTHON}" -m supervision {SERVICE_FLAG}', TOP)]
    assert ("assign_to_job", "job", "process-1") in api.calls


def test_a_supervisor_that_exits_is_restarted_after_a_doubling_wait() -> None:
    api = _api(lives=[0, 0, 0], polls_before_stop=4)

    _service(api).run()

    assert api.names().count("spawn") == 4
    assert api.restart_waits() == [RESTART_FIRST_SECONDS, RESTART_FIRST_SECONDS * 2, RESTART_FIRST_SECONDS * 4]


def test_the_restart_wait_is_capped() -> None:
    api = _api(lives=[0] * 12, polls_before_stop=13)

    _service(api).run()

    assert max(api.restart_waits()) == RESTART_CAP_SECONDS


def test_a_supervisor_that_ran_long_enough_restarts_from_the_first_wait() -> None:
    stable_polls = int(STABLE_RUN_SECONDS * 1000 / POLL_MILLISECONDS)
    api = _api(lives=[0, 0, stable_polls, 0], polls_before_stop=stable_polls + 5)

    _service(api).run()

    first, doubled = RESTART_FIRST_SECONDS, RESTART_FIRST_SECONDS * 2
    assert api.restart_waits() == [first, doubled, first, doubled]


def test_an_exited_supervisors_handle_is_closed() -> None:
    api = _api(lives=[0], polls_before_stop=2)

    _service(api).run()

    assert ("close", "process-1") in api.calls


def test_no_console_session_waits_and_never_fails() -> None:
    api = _api(session=NO_CONSOLE_SESSION)
    sleeps_before_stop = 5

    def sleep(seconds: float) -> None:
        api.clock.sleep(seconds)
        if len(api.clock.slept) >= sleeps_before_stop:
            api.stop.set()

    svc = Service(api, api.stop, application=PYTHON, cwd=TOP, sleep=sleep, monotonic=api.clock.monotonic)

    assert svc.run() == 0
    assert "spawn" not in api.names()


def test_the_waits_are_taken_in_poll_sized_slices() -> None:
    """A stop must not sit out a 60 s retry: every sleep is at most one poll, however long the wait."""
    api = _api(session=NO_CONSOLE_SESSION)
    long_enough_to_double_twice = NO_SESSION_RETRY_FIRST_SECONDS * 7

    def sleep(seconds: float) -> None:
        api.clock.sleep(seconds)
        if sum(api.clock.slept) >= long_enough_to_double_twice:
            api.stop.set()

    Service(api, api.stop, application=PYTHON, cwd=TOP, sleep=sleep, monotonic=api.clock.monotonic).run()

    assert max(api.clock.slept) <= POLL_MILLISECONDS / 1000


def test_a_stop_signals_the_event_then_waits_the_grace_then_closes_the_job() -> None:
    api = _api(polls_before_stop=1)

    _service(api).run()

    signal_at = api.calls.index(("signal", "stop-event"))
    grace_wait = ("wait", "process-1", int(SUPERVISOR_STOP_GRACE_SECONDS * 1000))
    assert grace_wait in api.calls
    assert signal_at < api.calls.index(grace_wait) < api.calls.index(("close", "job"))


def test_the_job_is_closed_even_when_the_supervisor_outlives_the_grace() -> None:
    api = _api(polls_before_stop=1, exits_on_stop_event=False)

    assert _service(api).run() == 0

    assert ("close", "job") in api.calls


def test_a_stop_with_no_supervisor_running_still_closes_the_job() -> None:
    api = _api(session=NO_CONSOLE_SESSION)
    api.stop.set()

    _service(api).run()

    assert "spawn" not in api.names()
    assert ("close", "job") in api.calls


def test_the_stop_event_is_the_supervisors() -> None:
    api = _api(polls_before_stop=1)

    _service(api).run()

    assert ("create_event", STOP_EVENT_NAME) in api.calls


def test_a_second_instance_exits_on_the_mutex_and_starts_nothing() -> None:
    api = _api(mutex_taken=True)

    assert _service(api).run() == ANOTHER_INSTANCE_EXIT_CODE

    assert api.calls == [("create_mutex", SERVICE_MUTEX_NAME)]


def test_the_mutex_is_taken_before_anything_else() -> None:
    api = _api(polls_before_stop=1)

    _service(api).run()

    assert api.calls[0] == ("create_mutex", SERVICE_MUTEX_NAME)


def test_the_grace_covers_the_supervisors_whole_teardown() -> None:
    """The copies the grace is built from, pinned to their sources: the app's shutdown wait,
    terminate-then-kill for each program in the teardown, and a probe the loop waits for at exit."""
    from common.config.supervisor import SupervisorConfig
    from supervision import probes, supervisor

    assert service.APP_SHUTDOWN_WAIT_SECONDS == probes.APP_SHUTDOWN_WAIT_SECONDS
    assert service.APP_STATUS_TIMEOUT_SECONDS == probes.APP_STATUS_TIMEOUT_SECONDS
    assert len(supervisor.TEARDOWN_ORDER) == service.TEARDOWN_PROGRAMS
    assert SupervisorConfig.model_fields["stop_grace_seconds"].default == service.PROGRAM_STOP_GRACE_SECONDS
    assert (
        probes.APP_SHUTDOWN_WAIT_SECONDS
        + len(supervisor.TEARDOWN_ORDER) * 2 * service.PROGRAM_STOP_GRACE_SECONDS
        + probes.APP_STATUS_TIMEOUT_SECONDS
    ) < SUPERVISOR_STOP_GRACE_SECONDS


def test_ctrl_c_sets_the_stop_instead_of_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    """NSSM stops its application with a console Ctrl-C; a KeyboardInterrupt in the middle of a
    Win32 wait would skip the teardown."""
    handlers: dict[int, Any] = {}
    monkeypatch.setattr(signal, "signal", lambda signum, handler: handlers.__setitem__(signum, handler))
    stop = threading.Event()

    install_stop_signals(stop)

    assert signal.SIGINT in handlers
    handlers[signal.SIGINT](signal.SIGINT, None)
    assert stop.is_set()


def test_the_log_rotates_in_its_own_folder(tmp_path: Path) -> None:
    folder = tmp_path / "mast-service"
    root = logging.getLogger()
    before = list(root.handlers)
    try:
        configure_logging(folder)
        logging.getLogger("supervision.service").info("hello")
        added = [h for h in root.handlers if h not in before]
        assert len(added) == 1
        assert isinstance(added[0], logging.handlers.RotatingFileHandler)
        added[0].flush()
        assert "hello" in (folder / service.LOG_FILE).read_text(encoding="utf-8")
    finally:
        for h in [h for h in root.handlers if h not in before]:
            root.removeHandler(h)
            h.close()
