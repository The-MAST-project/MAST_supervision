import logging
import threading
from collections.abc import Mapping
from concurrent.futures import Future
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from common.config.supervisor import SupervisorConfig
from supervision.clock import Clock
from supervision.launcher import LaunchSettings
from supervision.managed import FoundProcess, RestartPolicy, comparable_path
from supervision.probes import HealthResult
from supervision.programs import ResolvedProgram
from supervision.resources import ProbeResult, Resource
from supervision.state import OpMode, Phase, ProcessState, ResourceState, SupervisionMode
from supervision.supervisor import ConfigStatus, LoadedConfig, ResourceSettings, Supervisor, Wiring

T0 = datetime(2026, 10, 11, 18, 0, tzinfo=UTC)
TOP = Path("/top")
PYTHON = Path("/top/.venv/bin/python")
APP = str(TOP / "unit" / "src" / "app.py")
PWI4 = "/vendor/PWI4.exe"
OURS = 1
BUDGET = 300.0
HEARTBEAT = 300.0
OK = ProbeResult(ResourceState.OK, "fine")
DOWN = ProbeResult(ResourceState.DOWN, "unreachable")
HEALTHY = HealthResult(True, "ok")
ENV = {"PATH": "/bin", "HTTP_PROXY": "http://proxy:8080"}


class FakeClock:
    """``drift`` moves time on every read, as a real clock does during a tick."""

    def __init__(self, drift: float = 0.0) -> None:
        self.seconds = 0.0
        self.drift = drift

    def advance(self, seconds: float) -> None:
        self.seconds += seconds

    def _monotonic(self) -> float:
        self.seconds += self.drift
        return self.seconds

    def clock(self) -> Clock:
        return Clock(monotonic=self._monotonic, wall=lambda: T0 + timedelta(seconds=self.seconds))


@dataclass
class Proc:
    argv: list[str]
    running: bool = True
    parent: int | None = None


@dataclass
class FakeApi:
    procs: dict[int, Proc] = field(default_factory=dict)
    spawns: list[tuple[list[str], Path, Mapping[str, str] | None]] = field(default_factory=list)
    stops: list[int] = field(default_factory=list)
    next_pid: int = 100

    def find(self, image: str, argument: str | None = None) -> list[FoundProcess]:
        return [
            FoundProcess(pid, OURS, float(pid), p.parent)
            for pid, p in self.procs.items()
            if p.running
            and Path(p.argv[0]).name.casefold() == image.casefold()
            and (argument is None or comparable_path(argument) in map(comparable_path, p.argv))
        ]

    def current_session(self) -> int:
        return OURS

    def spawn(self, argv: list[str], cwd: Path, env: Mapping[str, str] | None = None) -> int:
        self.spawns.append((argv, cwd, env))
        pid = self.next_pid
        self.next_pid += 1
        self.procs[pid] = Proc(argv)
        return pid

    def is_running(self, pid: int) -> bool:
        return self.procs[pid].running

    def exit_code(self, pid: int) -> int | None:
        return None

    def terminate(self, pid: int) -> None:
        self.stops.append(pid)

    def kill(self, pid: int) -> None:
        self.stops.append(pid)

    def spawned(self, name: str) -> list[list[str]]:
        """The spawns whose program or any argument is a file called ``name``."""
        return [argv for argv, _, _ in self.spawns if any(Path(arg).name == name for arg in argv)]


def run_now(fn) -> Future:
    future: Future = Future()
    future.set_result(fn())
    return future


@dataclass
class World:
    opmode: OpMode = OpMode.OPERATED
    resource: ProbeResult = OK
    config_failures: int = 0
    problem: str = ""
    program_mode: SupervisionMode = SupervisionMode.SUPERVISED
    degraded_reason: str | None = None
    role: str = "unit"
    no_app: bool = False
    vscode: Path | None = Path("/vscode/Code.exe")
    api: FakeApi = field(default_factory=FakeApi)
    fake: FakeClock = field(default_factory=FakeClock)
    config_attempts: int = 0

    def load_config(self) -> LoadedConfig:
        self.config_attempts += 1
        if self.config_attempts <= self.config_failures:
            raise ConnectionError("mongo down")
        return LoadedConfig(
            opmode=self.opmode,
            supervisor=SupervisorConfig(heartbeat_interval_seconds=HEARTBEAT),
            app_port=8000,
            status=self.status(),
        )

    def status(self) -> ConfigStatus:
        return ConfigStatus(generation=418, degraded_reason=self.degraded_reason, in_maintenance=False)

    def programs(self, _loaded: LoadedConfig) -> list[ResolvedProgram]:
        policy = RestartPolicy()
        return [ResolvedProgram("pwi4", self.program_mode, (PWI4,), lambda: HEALTHY, policy, self.problem)]

    def supervisor(self) -> Supervisor:
        wiring = Wiring(
            clock=self.fake.clock(),
            api=self.api,
            resource_probes={Resource.NETWORK: lambda: self.resource},
            resources=ResourceSettings(wait_budget_seconds=BUDGET, probe_interval_seconds=5.0),
            load_config=self.load_config,
            config_status=self.status,
            resolve_programs=self.programs,
            launch_settings=lambda _: LaunchSettings(python=PYTHON, vscode_exe=self.vscode, workspace="mast-{role}.ws"),
            app_probe=lambda port: HEALTHY,
            heartbeat_seconds=HEARTBEAT,
            submit=run_now,
        )
        return Supervisor(self.role, "mast07", TOP, ENV, wiring, no_app=self.no_app)


def ticks(world: World, supervisor: Supervisor, n: int, step: float = 1.0) -> list[Phase]:
    phases = []
    for _ in range(n):
        supervisor.tick()
        phases.append(supervisor.snapshot.phase)
        world.fake.advance(step)
    return phases


def row(supervisor: Supervisor, name: str):
    return next(p for p in supervisor.snapshot.processes if p.name == name)


# --- phases ------------------------------------------------------------------------------------


def test_a_healthy_machine_walks_every_phase_to_running() -> None:
    world = World()
    phases = ticks(world, world.supervisor(), 6)

    assert phases == [
        Phase.WAITING_FOR_RESOURCES,
        Phase.LOADING_CONFIG,
        Phase.SUPERVISING,
        Phase.LAUNCHING_APP,
        Phase.RUNNING,
        Phase.RUNNING,
    ]


def test_a_resource_still_down_after_its_budget_is_waited_out_and_the_machine_runs_degraded() -> None:
    world = World(resource=DOWN)
    supervisor = world.supervisor()

    assert set(ticks(world, supervisor, 3, step=BUDGET / 4)) == {Phase.WAITING_FOR_RESOURCES}
    phases = ticks(world, supervisor, 6, step=BUDGET / 4)

    assert phases[-1] is Phase.DEGRADED
    assert row(supervisor, "pwi4").state is ProcessState.HEALTHY


def test_a_resource_that_comes_back_turns_degraded_into_running() -> None:
    world = World(resource=DOWN)
    supervisor = world.supervisor()
    ticks(world, supervisor, 10, step=BUDGET / 4)
    world.resource = OK

    assert ticks(world, supervisor, 2, step=10.0)[-1] is Phase.RUNNING


def test_a_configuration_that_cannot_be_loaded_is_retried_and_never_fatal() -> None:
    world = World(config_failures=2)
    supervisor = world.supervisor()

    phases = ticks(world, supervisor, 60)

    assert world.config_attempts == 3
    assert phases[-1] is Phase.RUNNING


def test_config_retries_back_off() -> None:
    world = World(config_failures=100)
    ticks(world, world.supervisor(), 30)

    assert world.config_attempts < 10


def test_a_program_that_cannot_be_started_is_a_row_saying_so() -> None:
    world = World(problem="PWI4.exe not found")
    supervisor = world.supervisor()
    ticks(world, supervisor, 6)

    status = row(supervisor, "pwi4")
    assert (status.state, status.detail) == (ProcessState.UNHEALTHY, "PWI4.exe not found")
    assert world.api.spawned("PWI4.exe") == []


def test_an_observed_program_that_cannot_be_resolved_keeps_its_mode() -> None:
    world = World(problem="no program named 'ascom'", program_mode=SupervisionMode.OBSERVED)
    supervisor = world.supervisor()
    ticks(world, supervisor, 6)

    assert row(supervisor, "pwi4").mode is SupervisionMode.OBSERVED


def test_a_configuration_that_recovers_from_its_boot_cache_turns_degraded_into_running() -> None:
    world = World(degraded_reason="served from the boot cache")
    supervisor = world.supervisor()
    assert ticks(world, supervisor, 6)[-1] is Phase.DEGRADED

    world.degraded_reason = None
    phases = ticks(world, supervisor, 2, step=10.0)

    assert phases[-1] is Phase.RUNNING
    assert supervisor.snapshot.config_degraded_reason is None


# --- the app -----------------------------------------------------------------------------------


def test_operated_launches_vscode_exactly_once() -> None:
    world = World()
    ticks(world, world.supervisor(), 50, step=30.0)

    assert world.api.spawned("Code.exe") == [["/vscode/Code.exe", str(TOP / "mast-unit.ws")]]


def test_operated_watches_the_app_as_not_supervised_until_it_appears() -> None:
    world = World()
    supervisor = world.supervisor()
    ticks(world, supervisor, 6)

    assert row(supervisor, "app").state is ProcessState.NOT_SUPERVISED
    assert row(supervisor, "vscode").state is ProcessState.NOT_SUPERVISED

    world.api.procs[999] = Proc([str(PYTHON), "-m", "debugpy", APP])
    ticks(world, supervisor, 3, step=20.0)

    app = row(supervisor, "app")
    assert (app.state, app.pid, app.mode) == (ProcessState.HEALTHY, 999, SupervisionMode.OBSERVED)
    assert world.api.spawned("app.py") == []


def test_a_launcher_and_the_app_it_runs_are_one_app_and_the_app_is_the_child() -> None:
    world = World()
    world.api.procs[900] = Proc([str(PYTHON), "debugpy/launcher", "--", APP])
    world.api.procs[901] = Proc([str(PYTHON), APP], parent=900)
    supervisor = world.supervisor()
    ticks(world, supervisor, 6)

    app = row(supervisor, "app")
    assert app.pid == 901
    assert "also running" not in app.detail


def test_another_python_is_not_mistaken_for_the_app() -> None:
    world = World()
    world.api.procs[555] = Proc([str(PYTHON), "-m", "supervision"])
    supervisor = world.supervisor()
    ticks(world, supervisor, 6)

    assert row(supervisor, "app").pid is None


def test_no_app_launches_neither_vscode_nor_the_app() -> None:
    for opmode in OpMode:
        world = World(opmode=opmode, no_app=True)
        supervisor = world.supervisor()
        ticks(world, supervisor, 20, step=30.0)

        assert world.api.spawned("Code.exe") == []
        assert world.api.spawned("app.py") == []
        assert row(supervisor, "app").state is ProcessState.NOT_SUPERVISED


def test_controlled_supervises_the_app_from_its_folder_without_the_proxy() -> None:
    world = World(opmode=OpMode.CONTROLLED)
    supervisor = world.supervisor()
    ticks(world, supervisor, 6)

    (argv, cwd, env) = next(spawn for spawn in world.api.spawns if APP in spawn[0])
    assert argv == [str(PYTHON), APP]
    assert cwd == TOP / "unit" / "src"
    assert env is not None and "HTTP_PROXY" not in env and env["PYTHONUNBUFFERED"] == "1"
    assert row(supervisor, "app").mode is SupervisionMode.SUPERVISED
    assert world.api.spawned("Code.exe") == []


def test_controlled_adopts_a_running_app_instead_of_starting_a_second() -> None:
    world = World(opmode=OpMode.CONTROLLED)
    world.api.procs[999] = Proc([str(PYTHON), APP])
    supervisor = world.supervisor()
    ticks(world, supervisor, 6)

    assert world.api.spawned("app.py") == []
    assert row(supervisor, "app").pid == 999


def test_controlled_does_not_adopt_another_python_as_the_app() -> None:
    world = World(opmode=OpMode.CONTROLLED)
    world.api.procs[555] = Proc([str(PYTHON), "-m", "supervision"])
    supervisor = world.supervisor()
    ticks(world, supervisor, 6)

    assert world.api.spawned("app.py") == [[str(PYTHON), APP]]
    assert row(supervisor, "app").pid != 555


def test_a_refused_launch_is_a_row_saying_so() -> None:
    world = World(opmode=OpMode.CONTROLLED, role="spec")
    supervisor = world.supervisor()
    ticks(world, supervisor, 6)

    app = row(supervisor, "app")
    assert app.state is ProcessState.UNHEALTHY
    assert "refused for spec" in app.detail


# --- reporting ---------------------------------------------------------------------------------


def test_a_heartbeat_follows_every_phase_change_and_then_the_interval(caplog) -> None:
    world = World()
    supervisor = world.supervisor()
    with caplog.at_level(logging.INFO, logger="mast.supervision.supervisor"):
        ticks(world, supervisor, 6)
        assert sum("supervisor heartbeat" in r.message for r in caplog.records) == 5
        caplog.clear()
        ticks(world, supervisor, int(HEARTBEAT) - 2)
        assert not any("supervisor heartbeat" in r.message for r in caplog.records)
        ticks(world, supervisor, 3)
        assert sum("supervisor heartbeat" in r.message for r in caplog.records) == 1


def test_the_heartbeat_follows_every_phase_change_on_a_clock_that_moves_during_a_tick(caplog) -> None:
    world = World(fake=FakeClock(drift=0.001))
    supervisor = world.supervisor()
    with caplog.at_level(logging.INFO, logger="mast.supervision.supervisor"):
        ticks(world, supervisor, 6)

    assert sum("supervisor heartbeat" in r.message for r in caplog.records) == 5


def test_a_quiet_loop_republishes_only_on_the_heartbeat() -> None:
    world = World()
    supervisor = world.supervisor()
    ticks(world, supervisor, 6)
    published = supervisor.snapshot

    ticks(world, supervisor, 10)
    assert supervisor.snapshot is published

    ticks(world, supervisor, int(HEARTBEAT))
    assert supervisor.snapshot.taken_at > published.taken_at


def test_the_snapshot_carries_the_configuration() -> None:
    world = World()
    supervisor = world.supervisor()
    ticks(world, supervisor, 6)

    snapshot = supervisor.snapshot
    assert (snapshot.opmode, snapshot.config_generation, snapshot.in_maintenance) == (OpMode.OPERATED, 418, False)


def test_stopping_leaves_the_programs_running() -> None:
    world = World(opmode=OpMode.CONTROLLED)
    supervisor = world.supervisor()
    stop = threading.Event()
    ticks(world, supervisor, 6)
    stop.set()

    supervisor.run(stop)

    assert world.api.stops == []
    assert all(p.running for p in world.api.procs.values())


@pytest.mark.parametrize("opmode", list(OpMode))
def test_the_programs_are_supervised_before_the_app_is_launched(opmode: OpMode) -> None:
    world = World(opmode=opmode)
    supervisor = world.supervisor()
    ticks(world, supervisor, 6)

    names = [Path(argv[0]).name for argv, _, _ in world.api.spawns]
    assert names[0] == "PWI4.exe"
