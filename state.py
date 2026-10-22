"""The supervisor's published snapshot: one model, three renderers.

The window's tables, the heartbeat block in the log and ``GET /status`` all render the
same :class:`SupervisorSnapshot`. The supervisor loop builds a new one and publishes it by
whole-reference assignment; readers never see live state, so a snapshot is frozen.

It is a Pydantic model so FastAPI serializes it with no second schema, and it imports
neither ``tkinter`` nor ``fastapi``, so it is tested on Linux CI.

:class:`SupervisionMode` and :class:`OpMode` are copies of MAST_common's
``common.config.supervisor.SupervisionMode`` and ``common.opmode.OpMode``: importing
``common.config`` builds a ``Filer`` and imports pymongo, which no consumer of this model
should pay for. ``tests/test_supervisor_state.py`` pins the copies to the originals;
consolidating them is MAST_supervision#2.

Design: mast-claude-config ``plans/supervisor-design.md`` §2, §5-§7.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from enum import IntEnum, StrEnum
from typing import Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, computed_field, model_validator

# The longest a tick can take: resource probes and the configuration load run on the loop
# thread, each bounded, and with the network down a tick can take several seconds.
STALE_MARGIN_SECONDS = 10.0


def is_stale(taken_at: datetime, heartbeat_seconds: float, now: datetime) -> bool:
    """True when a snapshot taken at ``taken_at`` means its loop has stopped publishing."""
    return now - taken_at > timedelta(seconds=heartbeat_seconds + STALE_MARGIN_SECONDS)


class SupervisionMode(StrEnum):
    SUPERVISED = "supervised"
    OBSERVED = "observed"
    DISABLED = "disabled"


class OpMode(StrEnum):
    DEVELOPMENT = "development"
    PRODUCTION = "production"


class Severity(IntEnum):
    """How bad a state is. The values are logging levels, so a heartbeat logs at its severity."""

    OK = logging.INFO
    WARNING = logging.WARNING
    ERROR = logging.ERROR


class Phase(StrEnum):
    STARTING = "starting"
    WAITING_FOR_RESOURCES = "waiting_for_resources"
    LOADING_CONFIG = "loading_config"
    SUPERVISING = "supervising"
    LAUNCHING_APP = "launching_app"
    RUNNING = "running"
    DEGRADED = "degraded"
    # Asked to stop: the loop has ended and the port is about to be released. A supervisor
    # starting meanwhile waits for the port instead of taking this one for a live peer.
    STOPPING = "stopping"


class ResourceState(StrEnum):
    OK = "ok"
    DEGRADED = "degraded"  # usable, but short of what it should be (some indexes missing)
    DOWN = "down"

    @property
    def severity(self) -> Severity:
        return Severity.OK if self is ResourceState.OK else Severity.WARNING


class ProcessState(StrEnum):
    STARTING = "starting"  # spawned or adopted, not yet probed healthy
    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"
    CRASH_LOOPED = "crash_looped"
    # A copy runs in another Windows login session: neither adopted nor killed, and no second
    # copy is started beside it. Clears by itself once that copy exits.
    BLOCKED = "blocked"
    # Launched once and deliberately not watched (VSCode under `development`). Not running is
    # its normal resting condition, so it is never a fault: a fleet view over /status that
    # reds out every developer machine is one nobody reads.
    NOT_SUPERVISED = "not_supervised"

    @property
    def severity(self) -> Severity:
        return _PROCESS_SEVERITY[self]


_PROCESS_SEVERITY = {
    ProcessState.HEALTHY: Severity.OK,
    ProcessState.NOT_SUPERVISED: Severity.OK,
    ProcessState.STARTING: Severity.WARNING,
    ProcessState.UNHEALTHY: Severity.WARNING,
    ProcessState.CRASH_LOOPED: Severity.ERROR,
    ProcessState.BLOCKED: Severity.ERROR,
}


class RevivalState(StrEnum):
    IDLE = "idle"
    RESTORING = "restoring"
    GIVING_UP = "giving_up"  # too many failed revivals lately: none is attempted

    @property
    def severity(self) -> Severity:
        return _REVIVAL_SEVERITY[self]


_REVIVAL_SEVERITY = {
    RevivalState.IDLE: Severity.OK,
    RevivalState.RESTORING: Severity.WARNING,
    RevivalState.GIVING_UP: Severity.ERROR,
}


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


class ResourceStatus(_Frozen):
    name: str
    state: ResourceState
    detail: str = ""  # shown verbatim, e.g. "47 indexes" or "3 of 47 indexes missing"
    since: AwareDatetime


class ProcessStatus(_Frozen):
    """One program's row. ``mode`` is ``None`` exactly when the program is not supervised."""

    name: str
    mode: SupervisionMode | None
    state: ProcessState
    pid: int | None = None
    restarts: int = 0
    last_exit_code: int | None = None
    detail: str = ""
    since: AwareDatetime  # when the program entered its current state

    @model_validator(mode="after")
    def _mode_agrees_with_state(self) -> Self:
        if (self.mode is None) != (self.state is ProcessState.NOT_SUPERVISED):
            raise ValueError(f"{self.name}: mode None and state {ProcessState.NOT_SUPERVISED} go together")
        if self.mode is SupervisionMode.DISABLED:
            raise ValueError(f"{self.name}: a disabled program is not reported")
        if self.mode is SupervisionMode.OBSERVED and self.state is ProcessState.CRASH_LOOPED:
            raise ValueError(f"{self.name}: an observed program is never restarted, so cannot crash-loop")
        return self


class RamdiskRevival(_Frozen):
    """The last time the RAM disk was lost and revived, so an operator can explain a gap in products."""

    state: RevivalState
    lost_at: AwareDatetime | None = None
    restored_at: AwareDatetime | None = None  # None while restoring, or when the last revival failed
    requested_by: str | None = None
    failures_in_window: int = 0
    detail: str = ""


class SupervisorSnapshot(_Frozen):
    """Everything the supervisor reports, at one instant.

    The config-derived fields are ``None`` until the config has loaded: the snapshot is
    published from ``STARTING`` on, and a machine that cannot reach the config database is
    exactly the one whose status matters most.
    """

    taken_at: AwareDatetime
    started_at: AwareDatetime
    phase: Phase
    hostname: str
    role: str
    # The interval in force: the snapshot is republished at least this often, so a reader
    # judges staleness from taken_at plus this, plus the longest tick (STALE_MARGIN_SECONDS).
    heartbeat_seconds: float
    opmode: OpMode | None = None
    in_maintenance: bool | None = None
    config_generation: int | None = None
    config_degraded_reason: str | None = None
    resources: tuple[ResourceStatus, ...] = ()
    processes: tuple[ProcessStatus, ...] = ()
    ramdisk_revival: RamdiskRevival | None = None  # None until the RAM disk is first lost

    def is_stale(self, now: datetime) -> bool:
        """True when the loop that published this has stopped publishing."""
        return is_stale(self.taken_at, self.heartbeat_seconds, now)

    @property
    def uptime(self) -> timedelta:
        return self.taken_at - self.started_at

    @computed_field
    @property
    def severity(self) -> Severity:
        """The worst state in the snapshot: what the heartbeat logs at and the GUI colors by."""
        states = [r.state.severity for r in self.resources] + [p.state.severity for p in self.processes]
        if self.ramdisk_revival is not None:
            states.append(self.ramdisk_revival.state.severity)
        return max(states, default=Severity.OK)
