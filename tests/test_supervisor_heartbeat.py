import logging
from datetime import UTC, datetime, timedelta

from supervision.heartbeat import render_heartbeat
from supervision.state import (
    OpMode,
    Phase,
    ProcessState,
    ProcessStatus,
    ResourceState,
    ResourceStatus,
    SupervisionMode,
    SupervisorSnapshot,
)

T0 = datetime(2026, 10, 11, 18, 0, tzinfo=UTC)


def resource(state: ResourceState, detail: str = "detail") -> ResourceStatus:
    return ResourceStatus(name="ramdisk", state=state, detail=detail, since=T0)


def process(state: ProcessState, detail: str = "detail") -> ProcessStatus:
    mode = None if state is ProcessState.NOT_SUPERVISED else SupervisionMode.SUPERVISED
    return ProcessStatus(name="pwi4", mode=mode, state=state, pid=5120, restarts=1, detail=detail, since=T0)


def snapshot(resources=(), processes=(), **fields) -> SupervisorSnapshot:
    return SupervisorSnapshot(
        taken_at=T0 + timedelta(hours=4, minutes=12, seconds=6, milliseconds=500),
        started_at=T0,
        phase=Phase.RUNNING,
        hostname="mast07",
        role="unit",
        resources=tuple(resources),
        processes=tuple(processes),
        **fields,
    )


def test_one_record_with_the_whole_state() -> None:
    _, text = render_heartbeat(
        snapshot(
            [resource(ResourceState.OK)],
            [process(ProcessState.HEALTHY)],
            opmode=OpMode.OPERATED,
            in_maintenance=False,
            config_generation=418,
        )
    )

    assert text.splitlines() == [
        "supervisor heartbeat  up 4:12:06  phase=running",
        "  role=unit  opmode=operated  maintenance=false  config gen=418",
        "  resources  ramdisk=ok",
        "  processes  pwi4=healthy pid=5120 r=1",
    ]


def test_before_the_configuration_loads_its_fields_read_unknown() -> None:
    _, text = render_heartbeat(snapshot())

    assert "opmode=?  maintenance=?  config gen=?" in text
    assert "resources  not yet probed" in text
    assert "processes  none yet" in text


def test_a_problem_carries_its_detail() -> None:
    _, text = render_heartbeat(
        snapshot([resource(ResourceState.DEGRADED, "3 of 47 indexes")], [process(ProcessState.UNHEALTHY, "refused")])
    )

    assert "ramdisk=degraded (3 of 47 indexes)" in text
    assert "pwi4=unhealthy pid=5120 r=1 (refused)" in text


def test_a_degraded_configuration_is_named() -> None:
    _, text = render_heartbeat(snapshot(config_degraded_reason="boot cache from 2026-10-10"))

    assert "config degraded: boot cache from 2026-10-10" in text


def test_the_text_is_pure_ascii() -> None:
    _, text = render_heartbeat(snapshot([resource(ResourceState.DOWN, "Z:\u2033 unreachable \u2192 retry")]))

    assert text.isascii()


def test_the_level_is_the_worst_state() -> None:
    assert render_heartbeat(snapshot([resource(ResourceState.OK)]))[0] == logging.INFO
    assert render_heartbeat(snapshot([resource(ResourceState.DEGRADED)]))[0] == logging.WARNING
    assert render_heartbeat(snapshot(processes=[process(ProcessState.CRASH_LOOPED)]))[0] == logging.ERROR


def test_not_supervised_is_no_fault() -> None:
    assert render_heartbeat(snapshot(processes=[process(ProcessState.NOT_SUPERVISED)]))[0] == logging.INFO
