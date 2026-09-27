import logging
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

import common.config.supervisor as common_supervisor
import common.opmode as common_opmode
from supervision.state import (
    OpMode,
    Phase,
    ProcessState,
    ProcessStatus,
    ResourceState,
    ResourceStatus,
    Severity,
    SupervisionMode,
    SupervisorSnapshot,
)

T0 = datetime(2026, 9, 27, 18, 0, tzinfo=UTC)


def _snapshot(resources=(), processes=()) -> SupervisorSnapshot:
    return SupervisorSnapshot(
        taken_at=T0 + timedelta(hours=4),
        started_at=T0,
        phase=Phase.RUNNING,
        hostname="mast07",
        role="unit",
        resources=resources,
        processes=processes,
    )


def _process(state: ProcessState, name: str = "pwi4", mode: SupervisionMode = SupervisionMode.SUPERVISED) -> ProcessStatus:
    return ProcessStatus(name=name, mode=mode, state=state)


def _vscode() -> ProcessStatus:
    return ProcessStatus(name="vscode", mode=None, state=ProcessState.NOT_SUPERVISED)


def _resource(state: ResourceState, name: str = "share") -> ResourceStatus:
    return ResourceStatus(name=name, state=state, since=T0)


@pytest.mark.parametrize(
    ("copy", "original"),
    [(SupervisionMode, common_supervisor.SupervisionMode), (OpMode, common_opmode.OpMode)],
)
def test_local_enum_copies_match_common(copy, original) -> None:
    # The copies exist only to keep common.config out of the import; see MAST_supervision#2.
    assert {m.name: m.value for m in copy} == {m.name: m.value for m in original}


def test_every_process_state_has_a_severity() -> None:
    for state in ProcessState:
        assert isinstance(state.severity, Severity)


def test_starting_snapshot_needs_no_config() -> None:
    snap = SupervisorSnapshot(taken_at=T0, started_at=T0, phase=Phase.STARTING, hostname="mast07", role="unit")
    assert snap.opmode is None
    assert snap.severity is Severity.OK


def test_all_green_is_ok() -> None:
    snap = _snapshot([_resource(ResourceState.OK)], [_process(ProcessState.HEALTHY)])
    assert snap.severity is Severity.OK


def test_not_supervised_is_not_a_fault() -> None:
    assert _snapshot(processes=[_vscode()]).severity is Severity.OK


def test_not_supervised_serializes_without_a_mode() -> None:
    row = _snapshot(processes=[_vscode()]).model_dump(mode="json")["processes"][0]
    assert row["mode"] is None
    assert row["state"] == "not_supervised"


def test_a_supervision_mode_is_not_not_supervised() -> None:
    with pytest.raises(ValidationError):
        _process(ProcessState.NOT_SUPERVISED)


def test_no_mode_is_only_for_not_supervised() -> None:
    with pytest.raises(ValidationError):
        ProcessStatus(name="pwi4", mode=None, state=ProcessState.HEALTHY)


def test_a_disabled_program_is_not_reported() -> None:
    with pytest.raises(ValidationError):
        _process(ProcessState.HEALTHY, mode=SupervisionMode.DISABLED)


def test_an_observed_program_cannot_crash_loop() -> None:
    with pytest.raises(ValidationError):
        _process(ProcessState.CRASH_LOOPED, "phd2", SupervisionMode.OBSERVED)


def test_naive_times_are_rejected() -> None:
    with pytest.raises(ValidationError):
        SupervisorSnapshot(
            taken_at=datetime(2026, 9, 27, 22), started_at=T0, phase=Phase.STARTING, hostname="mast07", role="unit"
        )


@pytest.mark.parametrize("state", [ResourceState.DEGRADED, ResourceState.DOWN])
def test_a_short_resource_is_a_warning(state: ResourceState) -> None:
    assert _snapshot([_resource(state)]).severity is Severity.WARNING


def test_the_worst_state_wins() -> None:
    snap = _snapshot(
        [_resource(ResourceState.DOWN)],
        [_process(ProcessState.UNHEALTHY), _process(ProcessState.CRASH_LOOPED, "ps3cli")],
    )
    assert snap.severity is Severity.ERROR


def test_severity_is_a_logging_level() -> None:
    assert _snapshot(processes=[_process(ProcessState.UNHEALTHY)]).severity == logging.WARNING


def test_uptime() -> None:
    assert _snapshot().uptime == timedelta(hours=4)


def test_is_frozen() -> None:
    snap = _snapshot()
    with pytest.raises(ValidationError):
        snap.phase = Phase.DEGRADED


def test_serializes_severity_for_the_status_api() -> None:
    dumped = _snapshot(processes=[_process(ProcessState.CRASH_LOOPED)]).model_dump(mode="json")
    assert dumped["severity"] == Severity.ERROR
    assert dumped["processes"][0]["state"] == "crash_looped"
