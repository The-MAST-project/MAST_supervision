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
from supervision.probes import HealthResult, ShutdownOutcome
from supervision.programs import ResolvedProgram
from supervision.ramdisk import RESTART_TASK, RamdiskReviver
from supervision.resources import ProbeResult, Resource
from supervision.state import OpMode, OpState, Phase, ProcessState, ResourceState, SupervisionMode
from supervision.supervisor import ConfigStatus, LoadedConfig, ResourceSettings, StopFlavor, Supervisor, Wiring

T0 = datetime(2026, 10, 11, 18, 0, tzinfo=UTC)
TOP = Path("/top")
PYTHON = Path("/top/.venv/bin/python")
APP = str(TOP / "unit" / "src" / "app.py")
PWI4 = "/vendor/PWI4.exe"
PHD2 = "/vendor/phd2.exe"
PS3CLI = "/vendor/ps3cli.exe"
APP_PORT = 8000
OURS = 1
BUDGET = 300.0
HEARTBEAT = 300.0
CONFIGURED_HEARTBEAT = 120.0
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
    survives_kill: bool = False

    @property
    def name(self) -> str:
        return "app.py" if APP in self.argv else Path(self.argv[0]).name


@dataclass
class FakeApi:
    procs: dict[int, Proc] = field(default_factory=dict)
    spawns: list[tuple[list[str], Path, Mapping[str, str] | None]] = field(default_factory=list)
    stops: list[int] = field(default_factory=list)
    events: list[str] = field(default_factory=list)  # terminations and app shutdowns, in order
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
        self.events.append(f"terminate {self.procs[pid].name}")
        if not self.procs[pid].survives_kill:
            self.procs[pid].running = False

    def kill(self, pid: int) -> None:
        self.stops.append(pid)
        if not self.procs[pid].survives_kill:
            self.procs[pid].running = False

    def spawned(self, name: str) -> list[list[str]]:
        """The spawns whose program or any argument is a file called ``name``."""
        return [argv for argv, _, _ in self.spawns if any(Path(arg).name == name for arg in argv)]


def run_now(fn) -> Future:
    future: Future = Future()
    future.set_result(fn())
    return future


@dataclass
class World:
    opmode: OpMode = OpMode.DEVELOPMENT
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
    ramdisk: ProbeResult | None = None  # None: no RAM disk probe
    ramdisk_runs: list[list[str]] = field(default_factory=list)
    full_set: bool = False  # PWI4, PHD2 observed and ps3cli, as a unit is configured
    app_opstate: OpState | None = None

    def load_config(self) -> LoadedConfig:
        self.config_attempts += 1
        if self.config_attempts <= self.config_failures:
            raise ConnectionError("mongo down")
        return LoadedConfig(
            opmode=self.opmode,
            supervisor=SupervisorConfig(heartbeat_interval_seconds=CONFIGURED_HEARTBEAT),
            app_port=APP_PORT,
            status=self.status(),
        )

    def status(self) -> ConfigStatus:
        return ConfigStatus(generation=418, degraded_reason=self.degraded_reason, in_maintenance=False)

    def programs(self, _loaded: LoadedConfig) -> list[ResolvedProgram]:
        policy = RestartPolicy()
        programs = [ResolvedProgram("pwi4", self.program_mode, (PWI4,), lambda: HEALTHY, policy, self.problem)]
        if self.full_set:
            programs += [
                ResolvedProgram("phd2", SupervisionMode.OBSERVED, (Path(PHD2).name,), lambda: HEALTHY, policy),
                ResolvedProgram("ps3cli", SupervisionMode.SUPERVISED, (PS3CLI, "--server"), lambda: HEALTHY, policy),
            ]
        return programs

    def app_health(self, port: int) -> HealthResult:
        if self.app_opstate is None:
            return HEALTHY
        return HealthResult(True, f"opstate={self.app_opstate}", self.app_opstate)

    def shutdown_app(self, port: int) -> ShutdownOutcome:
        self.api.events.append(f"shutdown {port}")
        return ShutdownOutcome(True, "opstate=shutdown")

    def ramdisk_probe(self) -> ProbeResult:
        assert self.ramdisk is not None
        return self.ramdisk

    def run_restart_task(self, argv) -> int:
        self.ramdisk_runs.append(list(argv))
        return 1

    def supervisor(self) -> Supervisor:
        probes = {Resource.NETWORK: lambda: self.resource}
        reviver = None
        if self.ramdisk is not None:
            probes[Resource.RAMDISK] = self.ramdisk_probe
            reviver = RamdiskReviver(
                lambda: self.ramdisk is OK,
                self.ramdisk_probe,
                self.run_restart_task,
                self.fake.clock(),
                sleep=self.fake.advance,
                start=lambda work: work(),
            )
        wiring = Wiring(
            clock=self.fake.clock(),
            api=self.api,
            resource_probes=probes,
            resources=ResourceSettings(wait_budget_seconds=BUDGET, probe_interval_seconds=5.0),
            load_config=self.load_config,
            config_status=self.status,
            resolve_programs=self.programs,
            launch_settings=lambda _: LaunchSettings(python=PYTHON, vscode_exe=self.vscode, workspace="mast-{role}.ws"),
            app_probe=self.app_health,
            heartbeat_seconds=HEARTBEAT,
            submit=run_now,
            ramdisk=reviver,
            app_shutdown=self.shutdown_app,
            sleep=self.fake.advance,
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


def test_development_launches_vscode_exactly_once() -> None:
    world = World()
    ticks(world, world.supervisor(), 50, step=30.0)

    assert world.api.spawned("Code.exe") == [[str(Path("/vscode/Code.exe")), str(TOP / "mast-unit.ws")]]


def test_development_watches_the_app_as_not_supervised_until_it_appears() -> None:
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


def test_production_supervises_the_app_from_its_folder_without_the_proxy() -> None:
    world = World(opmode=OpMode.PRODUCTION)
    supervisor = world.supervisor()
    ticks(world, supervisor, 6)

    (argv, cwd, env) = next(spawn for spawn in world.api.spawns if APP in spawn[0])
    assert argv == [str(PYTHON), APP]
    assert cwd == TOP / "unit" / "src"
    assert env is not None and "HTTP_PROXY" not in env and env["PYTHONUNBUFFERED"] == "1"
    assert row(supervisor, "app").mode is SupervisionMode.SUPERVISED
    assert world.api.spawned("Code.exe") == []


def test_production_adopts_a_running_app_instead_of_starting_a_second() -> None:
    world = World(opmode=OpMode.PRODUCTION)
    world.api.procs[999] = Proc([str(PYTHON), APP])
    supervisor = world.supervisor()
    ticks(world, supervisor, 6)

    assert world.api.spawned("app.py") == []
    assert row(supervisor, "app").pid == 999


def test_production_does_not_adopt_another_python_as_the_app() -> None:
    world = World(opmode=OpMode.PRODUCTION)
    world.api.procs[555] = Proc([str(PYTHON), "-m", "supervision"])
    supervisor = world.supervisor()
    ticks(world, supervisor, 6)

    assert world.api.spawned("app.py") == [[str(PYTHON), APP]]
    assert row(supervisor, "app").pid != 555


def test_a_refused_launch_is_a_row_saying_so() -> None:
    world = World(opmode=OpMode.PRODUCTION, role="spec")
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
        ticks(world, supervisor, int(CONFIGURED_HEARTBEAT) - 2)
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

    ticks(world, supervisor, int(CONFIGURED_HEARTBEAT))
    assert supervisor.snapshot.taken_at > published.taken_at


def test_the_snapshot_carries_the_configuration() -> None:
    world = World()
    supervisor = world.supervisor()
    ticks(world, supervisor, 6)

    snapshot = supervisor.snapshot
    assert (snapshot.opmode, snapshot.config_generation, snapshot.in_maintenance) == (OpMode.DEVELOPMENT, 418, False)


def test_stopping_leaves_the_programs_running() -> None:
    world = World(opmode=OpMode.PRODUCTION)
    supervisor = world.supervisor()
    stop = threading.Event()
    ticks(world, supervisor, 6)
    stop.set()

    supervisor.run(stop)

    assert world.api.stops == []
    assert all(p.running for p in world.api.procs.values())


def test_stopping_publishes_stopping_so_a_new_supervisor_waits() -> None:
    world = World()
    supervisor = world.supervisor()
    stop = threading.Event()
    stop.set()

    supervisor.run(stop)

    assert supervisor.snapshot.phase is Phase.STOPPING


def test_ctrl_c_publishes_stopping_too(monkeypatch) -> None:
    supervisor = World().supervisor()

    def interrupted() -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(supervisor, "tick", interrupted)
    with pytest.raises(KeyboardInterrupt):
        supervisor.run(threading.Event())

    assert supervisor.snapshot.phase is Phase.STOPPING


def test_the_snapshot_carries_the_heartbeat_interval_in_force() -> None:
    world = World()
    supervisor = world.supervisor()
    assert supervisor.snapshot.heartbeat_seconds == HEARTBEAT

    ticks(world, supervisor, 6)

    assert supervisor.snapshot.heartbeat_seconds == CONFIGURED_HEARTBEAT


@pytest.mark.parametrize("opmode", list(OpMode))
def test_the_programs_are_supervised_before_the_app_is_launched(opmode: OpMode) -> None:
    world = World(opmode=opmode)
    supervisor = world.supervisor()
    ticks(world, supervisor, 6)

    names = [Path(argv[0]).name for argv, _, _ in world.api.spawns]
    assert names[0] == "PWI4.exe"


# --- the RAM disk ------------------------------------------------------------------------------


def test_a_ramdisk_lost_while_running_is_revived_without_being_asked() -> None:
    world = World(ramdisk=OK)
    supervisor = world.supervisor()
    ticks(world, supervisor, 6)
    world.ramdisk = DOWN

    ticks(world, supervisor, 6)

    assert world.ramdisk_runs[:1] == [["schtasks", "/run", "/tn", RESTART_TASK]]
    assert supervisor.snapshot.ramdisk_revival is not None


def test_a_ramdisk_missing_during_the_resource_wait_is_not_revived() -> None:
    world = World(ramdisk=DOWN)
    supervisor = world.supervisor()

    phases = ticks(world, supervisor, 3, step=BUDGET / 4)

    assert set(phases) == {Phase.WAITING_FOR_RESOURCES}
    assert world.ramdisk_runs == []
    assert supervisor.snapshot.ramdisk_revival is None


# --- the teardown ------------------------------------------------------------------------------

TORN_DOWN_IN_ORDER = ["terminate app.py", "terminate phd2.exe", "terminate PWI4.exe", "terminate ps3cli.exe"]


def _running_unit(**kwargs) -> tuple[World, Supervisor]:
    world = World(opmode=OpMode.PRODUCTION, full_set=True, **kwargs)
    world.api.procs[50] = Proc([PHD2])  # PHD2 is the app's to start; the supervisor only observes it
    supervisor = world.supervisor()
    ticks(world, supervisor, 8, step=20.0)
    return world, supervisor


def _tear_down(supervisor: Supervisor) -> None:
    stop = threading.Event()
    stop.set()
    supervisor.run(stop, StopFlavor.TEAR_DOWN)


def test_a_teardown_stops_the_app_then_phd2_pwi4_and_ps3cli() -> None:
    world, supervisor = _running_unit()

    _tear_down(supervisor)

    assert world.api.events == TORN_DOWN_IN_ORDER
    for name in ("app", "phd2", "pwi4", "ps3cli"):
        assert row(supervisor, name).state is ProcessState.STOPPED
    assert supervisor.snapshot.phase is Phase.STOPPING


def test_nothing_is_respawned_after_a_teardown() -> None:
    world, supervisor = _running_unit()
    spawned = len(world.api.spawns)

    _tear_down(supervisor)
    world.fake.advance(600.0)
    supervisor.tick()

    assert len(world.api.spawns) == spawned


def test_a_started_up_app_is_shut_down_before_it_is_stopped() -> None:
    world, supervisor = _running_unit(app_opstate=OpState.STARTED_UP)

    _tear_down(supervisor)

    assert world.api.events == [f"shutdown {APP_PORT}", *TORN_DOWN_IN_ORDER]


@pytest.mark.parametrize("opstate", [OpState.INITIALIZED, OpState.SHUT_DOWN, None])
def test_an_app_not_started_up_is_never_sent_a_shutdown(opstate: OpState | None) -> None:
    world, supervisor = _running_unit(app_opstate=opstate)

    _tear_down(supervisor)

    assert world.api.events == TORN_DOWN_IN_ORDER


def test_a_program_that_survives_kill_does_not_hold_up_the_rest() -> None:
    world, supervisor = _running_unit()
    pwi4 = next(pid for pid, p in world.api.procs.items() if p.name == "PWI4.exe")
    world.api.procs[pwi4].survives_kill = True

    _tear_down(supervisor)

    assert world.api.events == TORN_DOWN_IN_ORDER
    assert row(supervisor, "pwi4").state is ProcessState.BLOCKED
    assert row(supervisor, "ps3cli").state is ProcessState.STOPPED


def test_an_adopted_app_is_torn_down_too() -> None:
    world = World(opmode=OpMode.PRODUCTION, full_set=True)
    world.api.procs[50] = Proc([PHD2])
    world.api.procs[999] = Proc([str(PYTHON), APP])
    supervisor = world.supervisor()
    ticks(world, supervisor, 8, step=20.0)
    assert row(supervisor, "app").pid == 999

    _tear_down(supervisor)

    assert world.api.events == TORN_DOWN_IN_ORDER


def test_a_loop_that_dies_leaves_the_programs_running_whatever_the_flavor(monkeypatch) -> None:
    world, supervisor = _running_unit()

    def broken() -> None:
        raise RuntimeError("a bug in a tick")

    monkeypatch.setattr(supervisor, "tick", broken)
    with pytest.raises(RuntimeError):
        supervisor.run(threading.Event(), StopFlavor.TEAR_DOWN)

    assert world.api.events == []
    assert supervisor.snapshot.phase is Phase.STOPPING


def test_leave_running_is_the_default_flavor() -> None:
    world, supervisor = _running_unit()
    stop = threading.Event()
    stop.set()

    supervisor.run(stop)

    assert world.api.events == []
