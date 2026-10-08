import logging
from concurrent.futures import Future
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from supervision.clock import Clock
from supervision.managed import STARTUP_GRACE_SECONDS, Backoff, FoundProcess, ManagedProcess, RestartPolicy
from supervision.probes import HealthResult
from supervision.state import ProcessState, Severity, SupervisionMode

T0 = datetime(2026, 10, 8, 18, 0, tzinfo=UTC)
OURS = 1
OTHER = 0
EXE = "C:/Program Files (x86)/PlaneWave Instruments/PWI4/PWI4.exe"
POLICY = RestartPolicy(probe_interval_seconds=10.0, stop_grace_seconds=10.0)
HEALTHY = HealthResult(True, "PWI4 4.1.6")
SICK = HealthResult(False, "refused")


class FakeClock:
    def __init__(self) -> None:
        self.seconds = 0.0

    def advance(self, seconds: float) -> None:
        self.seconds += seconds

    def clock(self) -> Clock:
        return Clock(monotonic=lambda: self.seconds, wall=lambda: T0 + timedelta(seconds=self.seconds))


@dataclass
class FakeProcess:
    session: int | None
    started: float = 0.0
    running: bool = True
    exit_code: int | None = None
    dies_on_terminate: bool = True


@dataclass
class FakeApi:
    processes: dict[int, FakeProcess] = field(default_factory=dict)
    spawns: list[tuple[list[str], Path]] = field(default_factory=list)
    terminated: list[int] = field(default_factory=list)
    killed: list[int] = field(default_factory=list)
    spawn_error: OSError | None = None
    signal_error: OSError | None = None
    images: list[str] = field(default_factory=list)
    next_pid: int = 100

    def find(self, image: str) -> list[FoundProcess]:
        self.images.append(image)
        return [FoundProcess(pid, p.session, p.started) for pid, p in self.processes.items() if p.running]

    def current_session(self) -> int:
        return OURS

    def spawn(self, argv: list[str], cwd: Path) -> int:
        if self.spawn_error is not None:
            raise self.spawn_error
        self.spawns.append((argv, cwd))
        pid = self.next_pid
        self.next_pid += 1
        self.processes[pid] = FakeProcess(session=OURS)
        return pid

    def is_running(self, pid: int) -> bool:
        return self.processes[pid].running

    def exit_code(self, pid: int) -> int | None:
        return self.processes[pid].exit_code

    def terminate(self, pid: int) -> None:
        if self.signal_error is not None:
            raise self.signal_error
        self.terminated.append(pid)
        if self.processes[pid].dies_on_terminate:
            self.processes[pid].running = False

    def kill(self, pid: int) -> None:
        self.killed.append(pid)
        self.processes[pid].running = False


class Probe:
    def __init__(self, result: HealthResult = HEALTHY) -> None:
        self.result = result
        self.calls = 0

    def __call__(self) -> HealthResult:
        self.calls += 1
        return self.result


def run_now(fn) -> Future:
    future: Future = Future()
    try:
        future.set_result(fn())
    except Exception as e:  # noqa: BLE001 -- the fake executor reports what the real one would
        future.set_exception(e)
    return future


def _managed(api: FakeApi, fake: FakeClock, probe: Probe, mode=SupervisionMode.SUPERVISED, submit=run_now):
    return ManagedProcess("pwi4", [EXE], mode, POLICY, probe, api, fake.clock(), submit=submit)


def _ticks(managed: ManagedProcess, fake: FakeClock, n: int, step: float = POLICY.probe_interval_seconds):
    status = managed.tick()
    for _ in range(n - 1):
        fake.advance(step)
        status = managed.tick()
    return status


# --- finding the program --------------------------------------------------------------------


def test_a_same_session_match_is_adopted_with_no_spawn_and_no_kill() -> None:
    api = FakeApi({7: FakeProcess(session=OURS)})
    status = _ticks(_managed(api, FakeClock(), Probe()), FakeClock(), 2)
    assert (api.spawns, api.terminated, api.killed) == ([], [], [])
    assert status.pid == 7
    assert status.state is ProcessState.HEALTHY


def test_an_adopted_unhealthy_program_is_not_killed_on_sight() -> None:
    api = FakeApi({7: FakeProcess(session=OURS)})
    fake = FakeClock()
    status = _managed(api, fake, Probe(SICK)).tick()
    assert (api.terminated, api.killed) == ([], [])
    assert status.state is ProcessState.UNHEALTHY


def test_the_oldest_of_several_same_session_copies_is_adopted_and_the_rest_reported() -> None:
    api = FakeApi({7: FakeProcess(session=OURS, started=50.0), 8: FakeProcess(session=OURS, started=10.0)})
    status = _managed(api, FakeClock(), Probe()).tick()
    assert status.pid == 8
    assert status.state is ProcessState.HEALTHY
    assert "also running here: pid 7" in status.detail


def test_a_copy_in_another_session_blocks_with_no_kill_and_no_spawn(caplog: pytest.LogCaptureFixture) -> None:
    api = FakeApi({7: FakeProcess(session=OTHER)})
    fake = FakeClock()
    managed = _managed(api, fake, Probe())
    with caplog.at_level(logging.ERROR):
        status = _ticks(managed, fake, 3)
    assert (api.spawns, api.terminated, api.killed) == ([], [], [])
    assert status.state is ProcessState.BLOCKED
    assert status.state.severity is Severity.ERROR
    assert "session 0" in status.detail
    assert len([r for r in caplog.records if r.levelno == logging.ERROR]) == 1


def test_an_unreadable_session_counts_as_another() -> None:
    api = FakeApi({7: FakeProcess(session=None)})
    assert _managed(api, FakeClock(), Probe()).tick().state is ProcessState.BLOCKED


def test_blocked_clears_and_spawns_once_the_other_copy_exits() -> None:
    api = FakeApi({7: FakeProcess(session=OTHER)})
    fake = FakeClock()
    managed = _managed(api, fake, Probe(SICK))
    managed.tick()
    api.processes[7].running = False
    fake.advance(POLICY.probe_interval_seconds)
    status = managed.tick()
    assert len(api.spawns) == 1
    assert status.state is ProcessState.STARTING


def test_ours_is_adopted_and_a_foreign_copy_reported() -> None:
    api = FakeApi({7: FakeProcess(session=OURS), 9: FakeProcess(session=OTHER)})
    status = _managed(api, FakeClock(), Probe()).tick()
    assert status.pid == 7
    assert "pid 9 in session 0" in status.detail


def test_nothing_running_is_spawned_in_the_executables_folder() -> None:
    api = FakeApi()
    status = _managed(api, FakeClock(), Probe(SICK)).tick()
    assert api.spawns == [([EXE], Path(EXE).parent)]
    assert status.state is ProcessState.STARTING


# --- observed mode --------------------------------------------------------------------------


def test_observed_never_spawns() -> None:
    api = FakeApi()
    fake = FakeClock()
    status = _ticks(_managed(api, fake, Probe(), SupervisionMode.OBSERVED), fake, 5)
    assert api.spawns == []
    assert status.state is ProcessState.UNHEALTHY


def test_observed_never_restarts_an_unhealthy_program() -> None:
    api = FakeApi({7: FakeProcess(session=OURS)})
    fake = FakeClock()
    status = _ticks(_managed(api, fake, Probe(SICK), SupervisionMode.OBSERVED), fake, 10)
    assert (api.spawns, api.terminated, api.killed) == ([], [], [])
    assert status.state is ProcessState.UNHEALTHY


def test_a_disabled_program_is_not_managed() -> None:
    with pytest.raises(ValueError, match="disabled"):
        _managed(FakeApi(), FakeClock(), Probe(), SupervisionMode.DISABLED)


# --- health, grace and restart --------------------------------------------------------------


def test_a_spawned_program_failing_within_its_grace_is_starting_not_restarted() -> None:
    api = FakeApi()
    fake = FakeClock()
    managed = _managed(api, fake, Probe(SICK))
    ticks = int(STARTUP_GRACE_SECONDS // POLICY.probe_interval_seconds)
    status = _ticks(managed, fake, ticks)
    assert api.terminated == []
    assert status.state is ProcessState.STARTING


def test_failures_count_once_the_grace_is_over() -> None:
    api = FakeApi()
    fake = FakeClock()
    managed = _managed(api, fake, Probe(SICK))
    managed.tick()
    fake.advance(STARTUP_GRACE_SECONDS)
    status = managed.tick()
    assert status.state is ProcessState.UNHEALTHY
    assert "(1/3)" in status.detail


def test_the_threshold_makes_exactly_one_restart() -> None:
    api = FakeApi({7: FakeProcess(session=OURS)})
    fake = FakeClock()
    managed = _managed(api, fake, Probe(SICK))
    _ticks(managed, fake, POLICY.unhealthy_threshold)
    assert api.terminated == [7]
    fake.advance(POLICY.restart_backoff_seconds)
    managed.tick()
    assert len(api.spawns) == 1


def test_a_process_that_ignores_terminate_is_killed_after_the_grace() -> None:
    api = FakeApi({7: FakeProcess(session=OURS, dies_on_terminate=False)})
    fake = FakeClock()
    managed = _managed(api, fake, Probe(SICK))
    _ticks(managed, fake, POLICY.unhealthy_threshold)
    fake.advance(POLICY.stop_grace_seconds - 1)
    managed.tick()
    assert api.killed == []
    fake.advance(1)
    managed.tick()
    assert api.killed == [7]


def test_an_exit_restarts_after_the_backoff_and_reports_the_exit_code() -> None:
    api = FakeApi()
    fake = FakeClock()
    managed = _managed(api, fake, Probe())
    managed.tick()
    api.processes[100].running, api.processes[100].exit_code = False, 3
    status = managed.tick()
    assert status.last_exit_code == 3
    assert "restarting in 5s" in status.detail
    assert len(api.spawns) == 1
    fake.advance(POLICY.restart_backoff_seconds)
    managed.tick()
    assert len(api.spawns) == 2


def test_more_restarts_than_allowed_in_the_window_stop_supervision() -> None:
    api = FakeApi()
    fake = FakeClock()
    managed = _managed(api, fake, Probe())
    for _ in range(POLICY.crash_loop_restarts + 1):
        managed.tick()
        pid = max(api.processes)
        api.processes[pid].running, api.processes[pid].exit_code = False, 1
        status = managed.tick()
        fake.advance(POLICY.restart_backoff_cap_seconds)
    assert status.state is ProcessState.CRASH_LOOPED
    assert "last exit code 1" in status.detail
    assert f"{POLICY.crash_loop_restarts + 1} exits" in status.detail
    spawned = len(api.spawns)
    fake.advance(POLICY.restart_backoff_cap_seconds)
    managed.tick()
    assert len(api.spawns) == spawned


def test_retry_leaves_crash_looped_and_spawns_again() -> None:
    api = FakeApi()
    fake = FakeClock()
    managed = _managed(api, fake, Probe())
    for _ in range(POLICY.crash_loop_restarts + 1):
        managed.tick()
        api.processes[max(api.processes)].running = False
        managed.tick()
        fake.advance(POLICY.restart_backoff_cap_seconds)
    spawned = len(api.spawns)
    managed.retry()
    status = managed.tick()
    assert len(api.spawns) == spawned + 1
    assert status.state is ProcessState.HEALTHY


def test_a_failed_spawn_is_retried_after_the_backoff() -> None:
    api = FakeApi(spawn_error=FileNotFoundError("PWI4.exe"))
    fake = FakeClock()
    managed = _managed(api, fake, Probe())
    status = managed.tick()
    assert "spawn failed" in status.detail
    api.spawn_error = None
    fake.advance(POLICY.restart_backoff_seconds)
    managed.tick()
    assert len(api.spawns) == 1


def test_backoff_doubles_and_caps() -> None:
    backoff = Backoff(5.0, 30.0)
    assert [backoff.next() for _ in range(5)] == [5.0, 10.0, 20.0, 30.0, 30.0]
    backoff.reset()
    assert backoff.next() == 5.0


# --- probing --------------------------------------------------------------------------------


def test_a_probe_still_running_is_never_joined_by_a_second() -> None:
    api = FakeApi({7: FakeProcess(session=OURS)})
    fake = FakeClock()
    submitted: list[Future] = []

    def hold(fn) -> Future:
        future: Future = Future()
        submitted.append(future)
        return future

    managed = _managed(api, fake, Probe(), submit=hold)
    _ticks(managed, fake, 5)
    assert len(submitted) == 1
    submitted[0].set_result(HEALTHY)
    assert managed.tick().state is ProcessState.HEALTHY


def test_a_raising_probe_is_a_failed_check() -> None:
    api = FakeApi({7: FakeProcess(session=OURS)})

    def boom() -> HealthResult:
        raise RuntimeError("probe bug")

    managed = ManagedProcess("pwi4", [EXE], SupervisionMode.SUPERVISED, POLICY, boom, api, FakeClock().clock(), run_now)
    status = managed.tick()
    assert status.state is ProcessState.UNHEALTHY
    assert "RuntimeError" in status.detail


def test_since_moves_only_when_the_state_changes() -> None:
    api = FakeApi({7: FakeProcess(session=OURS)})
    fake = FakeClock()
    managed = _managed(api, fake, Probe())
    first = managed.tick()
    fake.advance(POLICY.probe_interval_seconds)
    second = managed.tick()
    assert first.state is second.state is ProcessState.HEALTHY
    assert first.since == second.since


# --- found by the review of part 4 ----------------------------------------------------------


def test_the_image_looked_for_is_the_executables_name() -> None:
    api = FakeApi()
    _managed(api, FakeClock(), Probe()).tick()
    assert set(api.images) == {"PWI4.exe"}


def _exits_once(api: FakeApi, fake: FakeClock) -> ManagedProcess:
    managed = _managed(api, fake, Probe())
    managed.tick()
    api.processes[100].running = False
    managed.tick()
    return managed


def test_a_foreign_copy_started_during_the_backoff_blocks_the_respawn() -> None:
    api = FakeApi()
    fake = FakeClock()
    managed = _exits_once(api, fake)
    api.processes[7] = FakeProcess(session=OTHER)
    fake.advance(POLICY.restart_backoff_seconds)
    status = managed.tick()
    assert len(api.spawns) == 1
    assert status.state is ProcessState.BLOCKED


def test_a_same_session_copy_started_during_the_backoff_is_adopted() -> None:
    api = FakeApi()
    fake = FakeClock()
    managed = _exits_once(api, fake)
    api.processes[7] = FakeProcess(session=OURS)
    fake.advance(POLICY.restart_backoff_seconds)
    status = managed.tick()
    assert len(api.spawns) == 1
    assert status.pid == 7


def test_a_process_that_refuses_terminate_is_blocked_and_not_stopped_again() -> None:
    api = FakeApi({7: FakeProcess(session=OURS)}, signal_error=PermissionError("pid 7: access denied"))
    fake = FakeClock()
    managed = _managed(api, fake, Probe(SICK))
    status = _ticks(managed, fake, POLICY.unhealthy_threshold + 3)
    assert status.state is ProcessState.BLOCKED
    assert "cannot be stopped" in status.detail
    assert api.spawns == []


def test_a_process_that_survives_kill_is_blocked() -> None:
    api = FakeApi({7: FakeProcess(session=OURS, dies_on_terminate=False)})
    fake = FakeClock()
    managed = _managed(api, fake, Probe(SICK))
    _ticks(managed, fake, POLICY.unhealthy_threshold)
    api.kill = lambda pid: api.killed.append(pid)  # the kill does nothing
    fake.advance(POLICY.stop_grace_seconds)
    managed.tick()
    fake.advance(POLICY.stop_grace_seconds)
    status = managed.tick()
    assert api.killed == [7]
    assert status.state is ProcessState.BLOCKED
    assert "survived kill" in status.detail


def test_terminate_kill_and_restart_are_logged(caplog: pytest.LogCaptureFixture) -> None:
    api = FakeApi({7: FakeProcess(session=OURS, dies_on_terminate=False)})
    fake = FakeClock()
    managed = _managed(api, fake, Probe(SICK))
    with caplog.at_level(logging.INFO):
        _ticks(managed, fake, POLICY.unhealthy_threshold)
        fake.advance(POLICY.stop_grace_seconds)
        managed.tick()
        fake.advance(POLICY.restart_backoff_seconds)
        managed.tick()
    text = "\n".join(r.getMessage() for r in caplog.records)
    for action in ("terminating pid 7", "killing pid 7", "restarting in", "spawned pid"):
        assert action in text


def test_a_copy_reported_beside_the_adopted_one_is_dropped_once_it_exits() -> None:
    api = FakeApi({7: FakeProcess(session=OURS, started=1.0), 8: FakeProcess(session=OURS, started=2.0)})
    fake = FakeClock()
    managed = _managed(api, fake, Probe())
    assert "pid 8" in managed.tick().detail
    api.processes[8].running = False
    fake.advance(POLICY.probe_interval_seconds)
    assert "pid 8" not in managed.tick().detail
