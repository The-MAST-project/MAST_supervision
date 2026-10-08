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
from datetime import timedelta
from enum import IntEnum, StrEnum
from typing import Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, computed_field, model_validator


class SupervisionMode(StrEnum):
    SUPERVISED = "supervised"
    OBSERVED = "observed"
    DISABLED = "disabled"


class OpMode(StrEnum):
    OPERATED = "operated"
    CONTROLLED = "controlled"


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
    # Launched once and deliberately not watched (VSCode under `operated`). Not running is
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
    opmode: OpMode | None = None
    in_maintenance: bool | None = None
    config_generation: int | None = None
    config_degraded_reason: str | None = None
    resources: tuple[ResourceStatus, ...] = ()
    processes: tuple[ProcessStatus, ...] = ()

    @property
    def uptime(self) -> timedelta:
        return self.taken_at - self.started_at

    @computed_field
    @property
    def severity(self) -> Severity:
        """The worst state in the snapshot: what the heartbeat logs at and the GUI colors by."""
        states = [r.state.severity for r in self.resources] + [p.state.severity for p in self.processes]
        return max(states, default=Severity.OK)
