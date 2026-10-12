"""The supervisor loop: wait for the machine, load the configuration, own the programs, launch the app.

``STARTING -> WAITING_FOR_RESOURCES -> LOADING_CONFIG -> SUPERVISING -> LAUNCHING_APP -> RUNNING``,
or ``DEGRADED`` in place of ``RUNNING`` while a resource is short or the configuration came from
its boot cache; ``STOPPING`` once asked to stop. One thread calls :meth:`Supervisor.tick` about
once a second. A program's health probe runs on that program's own executor and never holds a
tick up; the resource probes and the configuration load run on the loop thread, each bounded, so
a tick on a machine whose network is down can take several seconds.

**Nothing is fatal.** A resource still down after its budget is waited out and kept probing; a
configuration that cannot be loaded is retried on a backoff; a program that cannot be found is
a row saying so. A supervisor that dies leaves a machine nobody can see.

**The rows** are one per program the configuration names, plus the app:

- ``development``: VSCode, launched once and never watched (``not_supervised``), and the app,
  watched read-only. The operator starts the app from VSCode, so an app not running is the
  resting state, reported ``not_supervised`` too, until it appears.
- ``production``: the app, supervised like PWI4. It is matched by its ``app.py`` on the command
  line, since its image is ``python.exe`` like the supervisor's own.
- ``--no-app``: nothing is launched; the app is watched as under ``development``.

**The snapshot** is rebuilt every tick and published, by whole-reference assignment, when it
differs from the last one -- and on every heartbeat even when it does not. So a ``taken_at``
older than the heartbeat interval plus the longest tick (``SupervisorSnapshot.is_stale``) means
the loop has stopped, however quiet the night.

**The configuration is watched.** Its generation, its degraded reason and this unit's
maintenance flag are re-read on every resource poll, so a supervisor that started on the boot
cache leaves ``DEGRADED`` once the database is back. ``opmode`` and the process list are read
once: changing what is launched is a restart (restart-to-apply).

**Stopping leaves the programs running.** Without the service there is one way to stop, and
it is the restart flavor: PWI4, ps3cli and the app stay up and are adopted on the next start.
Stopping them in order belongs to the service (stage 5).

The resource wait runs before the configuration can be read, so its budget, interval and index
set are ``ResourcesConfig``'s defaults, passed in by the caller.

Design: mast-claude-config ``plans/supervisor-design.md`` §5-§8.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from common.mast_logging import get_logger
from supervision.clock import Clock
from supervision.heartbeat import render_heartbeat
from supervision.launcher import LaunchError, LaunchSettings, LaunchSpec, app_entry, build_launch
from supervision.managed import Backoff, ManagedProcess, ProcessApi, RestartPolicy, Submit
from supervision.probes import HealthResult
from supervision.programs import ResolvedProgram
from supervision.resources import Probe, Resource, ResourceTracker, WaitOutcome
from supervision.state import (
    OpMode,
    Phase,
    ProcessState,
    ProcessStatus,
    ResourceState,
    SupervisionMode,
    SupervisorSnapshot,
)

if TYPE_CHECKING:
    from common.config.supervisor import SupervisorConfig

logger = get_logger(__name__)

TICK_SECONDS = 1.0
CONFIG_RETRY_FIRST_SECONDS = 5.0
CONFIG_RETRY_CAP_SECONDS = 60.0
APP = "app"
NOT_STARTED_BY_US = "not running (the operator starts it from VSCode)"


@dataclass(frozen=True, slots=True)
class ConfigStatus:
    """What the configuration says about itself and this machine, re-read while running."""

    generation: int
    degraded_reason: str | None
    in_maintenance: bool | None  # None on a spec machine


@dataclass(frozen=True, slots=True)
class LoadedConfig:
    """What the loop reads from the configuration once, when it loads."""

    opmode: OpMode
    supervisor: SupervisorConfig
    app_port: int
    status: ConfigStatus


@dataclass(frozen=True, slots=True)
class ResourceSettings:
    wait_budget_seconds: float
    probe_interval_seconds: float


@dataclass(frozen=True, slots=True)
class Wiring:
    """Everything the loop reaches outside itself, so every phase is tested against fakes."""

    clock: Clock
    api: ProcessApi
    resource_probes: Mapping[Resource, Probe]
    resources: ResourceSettings
    load_config: Callable[[], LoadedConfig]
    config_status: Callable[[], ConfigStatus]  # re-read once loaded
    resolve_programs: Callable[[LoadedConfig], list[ResolvedProgram]]
    launch_settings: Callable[[LoadedConfig], LaunchSettings]
    app_probe: Callable[[int], HealthResult]  # the app's port -> its health
    heartbeat_seconds: float  # until the configuration says otherwise
    submit: Submit | None = None  # how probes run; None: each row's own executor


class Row(Protocol):
    name: str

    def tick(self) -> ProcessStatus: ...


@dataclass(slots=True)
class FixedRow:
    """A row that cannot change until the supervisor restarts: a program that cannot be started."""

    name: str
    status: ProcessStatus

    def tick(self) -> ProcessStatus:
        return self.status

    @classmethod
    def problem(cls, name: str, mode: SupervisionMode, detail: str, since: datetime) -> FixedRow:
        logger.error(f"{name}: {detail}")
        status = ProcessStatus(name=name, mode=mode, state=ProcessState.UNHEALTHY, detail=detail, since=since)
        return cls(name, status)

    @classmethod
    def launch_once(cls, spec: LaunchSpec, api: ProcessApi, since: datetime) -> FixedRow:
        """Start ``spec`` now and never look at it again."""
        try:
            pid = api.spawn(list(spec.argv), spec.cwd, spec.env)
        except OSError as e:
            logger.error(f"{spec.name}: launch failed: {e}")
            detail = f"launch failed: {e}"
        else:
            logger.info(f"{spec.name}: launched pid {pid}, not supervised: {list(spec.argv)}")
            detail = f"launched pid {pid}"
        status = ProcessStatus(name=spec.name, mode=None, state=ProcessState.NOT_SUPERVISED, detail=detail, since=since)
        return cls(spec.name, status)


@dataclass(slots=True)
class WatchedApp:
    """The app when the operator starts it: watched, never started, and absent at rest."""

    inner: ManagedProcess
    name: str = APP

    def tick(self) -> ProcessStatus:
        status = self.inner.tick()
        if status.pid is not None or status.state is ProcessState.BLOCKED:
            return status
        return ProcessStatus(
            name=status.name,
            mode=None,
            state=ProcessState.NOT_SUPERVISED,
            restarts=status.restarts,
            last_exit_code=status.last_exit_code,
            detail=NOT_STARTED_BY_US,
            since=status.since,
        )


@dataclass(slots=True)
class _ConfigAttempts:
    backoff: Backoff = field(default_factory=lambda: Backoff(CONFIG_RETRY_FIRST_SECONDS, CONFIG_RETRY_CAP_SECONDS))
    next_at: float = 0.0


class Supervisor:
    def __init__(self, role: str, hostname: str, top: Path, env: Mapping[str, str], wiring: Wiring, no_app: bool) -> None:
        self._role = role
        self._hostname = hostname
        self._top = top
        self._env = env
        self._wiring = wiring
        self._no_app = no_app
        self._clock = wiring.clock
        self._started_at = self._clock.wall()
        self._tracker = ResourceTracker(wiring.resource_probes, wiring.resources.wait_budget_seconds, self._clock)
        self._next_resource_poll = 0.0
        self._config_attempts = _ConfigAttempts()
        self._loaded: LoadedConfig | None = None
        self._config_status: ConfigStatus | None = None
        self._rows: list[Row] = []
        self._statuses: tuple[ProcessStatus, ...] = ()
        self._phase = Phase.STARTING
        self._heartbeat_due = 0.0
        self.snapshot = self._build_snapshot()

    def run(self, stop: threading.Event) -> None:
        """Tick until ``stop`` is set. The programs are left running."""
        logger.info(f"supervisor starting: role={self._role} host={self._hostname} top={self._top}")
        try:
            while True:
                self.tick()
                if stop.wait(TICK_SECONDS):
                    break
        finally:
            self._stopping()

    def _stopping(self) -> None:
        """Publish ``STOPPING`` so a supervisor starting meanwhile waits rather than aborts."""
        logger.info("supervisor stopping; the programs it owns are left running and are adopted on the next start")
        now = self._clock.monotonic()
        self._enter(Phase.STOPPING, now)
        self._publish(now)

    def tick(self) -> None:
        now = self._clock.monotonic()
        match self._phase:
            case Phase.STARTING:
                self._enter(Phase.WAITING_FOR_RESOURCES, now)
            case Phase.WAITING_FOR_RESOURCES:
                self._poll_resources(now)
                if self._tracker.outcome() is not WaitOutcome.WAITING:
                    self._enter(Phase.LOADING_CONFIG, now)
            case Phase.LOADING_CONFIG:
                self._poll_resources(now)
                if self._try_config(now):
                    self._enter(Phase.SUPERVISING, now)
            case Phase.SUPERVISING:
                self._poll_resources(now)
                self._tick_rows()
                self._enter(Phase.LAUNCHING_APP, now)
            case Phase.LAUNCHING_APP:
                self._poll_resources(now)
                self._rows.append(self._app_row())
                self._tick_rows()
                self._enter(self._steady_phase(), now)
            case Phase.RUNNING | Phase.DEGRADED:
                self._poll_resources(now)
                self._tick_rows()
                self._enter(self._steady_phase(), now)
        self._publish(now)

    # --- phases ------------------------------------------------------------------------------

    def _enter(self, phase: Phase, now: float) -> None:
        if phase is self._phase:
            return
        logger.info(f"supervisor phase: {self._phase} -> {phase}")
        self._phase = phase
        self._heartbeat_due = now  # a state block in this very tick

    def _poll_resources(self, now: float) -> None:
        if now >= self._next_resource_poll:
            self._next_resource_poll = now + self._wiring.resources.probe_interval_seconds
            self._tracker.poll()
            if self._loaded is not None:
                self._refresh_config_status(self._wiring.config_status())

    def _refresh_config_status(self, status: ConfigStatus) -> None:
        previous = self._config_status
        if previous is not None and status.degraded_reason != previous.degraded_reason:
            if status.degraded_reason:
                logger.warning(f"configuration degraded: {status.degraded_reason}")
            else:
                logger.info("configuration healthy again")
        self._config_status = status

    def _try_config(self, now: float) -> bool:
        attempts = self._config_attempts
        if now < attempts.next_at:
            return False
        try:
            loaded = self._wiring.load_config()
        except Exception as e:  # noqa: BLE001 -- an unreadable configuration is retried, never fatal
            delay = attempts.backoff.next()
            attempts.next_at = now + delay
            logger.warning(f"configuration not loaded ({type(e).__name__}: {e}); retrying in {delay:g}s", exc_info=True)
            return False
        self._loaded = loaded
        self._config_status = loaded.status
        if loaded.status.degraded_reason:
            logger.warning(f"configuration degraded: {loaded.status.degraded_reason}")
        self._rows = [self._program_row(program) for program in self._wiring.resolve_programs(loaded)]
        return True

    def _program_row(self, program: ResolvedProgram) -> Row:
        if program.problem:
            return FixedRow.problem(program.name, program.mode, program.problem, self._clock.wall())
        return ManagedProcess(
            program.name,
            list(program.argv),
            program.mode,
            program.policy,
            program.probe,
            self._wiring.api,
            self._clock,
            submit=self._wiring.submit,
        )

    def _app_row(self) -> Row:
        loaded = self._require_loaded()
        if self._no_app:
            logger.info("--no-app: nothing is launched; the app is watched")
            return self._watched_app(loaded)
        try:
            spec = build_launch(self._role, loaded.opmode, self._top, self._wiring.launch_settings(loaded), self._env)
        except LaunchError as e:
            return FixedRow.problem(APP, SupervisionMode.SUPERVISED, f"not launched: {e}", self._clock.wall())
        if not spec.supervised:
            self._rows.append(FixedRow.launch_once(spec, self._wiring.api, self._clock.wall()))
            return self._watched_app(loaded)
        return ManagedProcess(
            APP,
            list(spec.argv),
            SupervisionMode.SUPERVISED,
            self._app_policy(loaded),
            self._app_health(loaded),
            self._wiring.api,
            self._clock,
            submit=self._wiring.submit,
            argument=spec.argv[-1],
            cwd=spec.cwd,
            env=spec.env,
        )

    def _watched_app(self, loaded: LoadedConfig) -> WatchedApp:
        app = str(app_entry(self._role, self._top))
        inner = ManagedProcess(
            APP,
            [str(self._wiring.launch_settings(loaded).python), app],
            SupervisionMode.OBSERVED,
            self._app_policy(loaded),
            self._app_health(loaded),
            self._wiring.api,
            self._clock,
            submit=self._wiring.submit,
            argument=app,
        )
        return WatchedApp(inner)

    def _app_policy(self, loaded: LoadedConfig) -> RestartPolicy:
        app = loaded.supervisor.app
        return RestartPolicy(probe_interval_seconds=app.probe_interval_seconds, stop_grace_seconds=app.stop_grace_seconds)

    def _app_health(self, loaded: LoadedConfig) -> Callable[[], HealthResult]:
        port = loaded.app_port
        probe = self._wiring.app_probe
        return lambda: probe(port)

    def _tick_rows(self) -> None:
        self._statuses = tuple(row.tick() for row in self._rows)

    def _steady_phase(self) -> Phase:
        short = any(status.state is not ResourceState.OK for status in self._tracker.statuses)
        degraded_config = self._config_status is not None and self._config_status.degraded_reason
        return Phase.DEGRADED if short or degraded_config else Phase.RUNNING

    def _require_loaded(self) -> LoadedConfig:
        if self._loaded is None:
            raise RuntimeError("the configuration is read before any program is started")
        return self._loaded

    # --- reporting ---------------------------------------------------------------------------

    def _build_snapshot(self) -> SupervisorSnapshot:
        loaded, status = self._loaded, self._config_status
        return SupervisorSnapshot(
            taken_at=self._clock.wall(),
            started_at=self._started_at,
            phase=self._phase,
            hostname=self._hostname,
            role=self._role,
            heartbeat_seconds=self._heartbeat_seconds(),
            opmode=loaded.opmode if loaded else None,
            in_maintenance=status.in_maintenance if status else None,
            config_generation=status.generation if status else None,
            config_degraded_reason=status.degraded_reason if status else None,
            resources=self._tracker.statuses,
            processes=self._statuses,
        )

    def _publish(self, now: float) -> None:
        snapshot = self._build_snapshot()
        heartbeat = now >= self._heartbeat_due
        if heartbeat or _content(snapshot) != _content(self.snapshot):
            self.snapshot = snapshot
        if heartbeat:
            level, text = render_heartbeat(snapshot)
            logger.log(level, text)
            self._heartbeat_due = now + self._heartbeat_seconds()

    def _heartbeat_seconds(self) -> float:
        return self._loaded.supervisor.heartbeat_interval_seconds if self._loaded else self._wiring.heartbeat_seconds


def _content(snapshot: SupervisorSnapshot) -> dict[str, object]:
    return snapshot.model_dump(exclude={"taken_at"})
