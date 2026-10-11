import logging
import tomllib
from collections import deque
from datetime import UTC, datetime, timedelta
from pathlib import Path

from supervision.gui_model import (
    LEVELS,
    TableRow,
    about_text,
    drain,
    header,
    help_text,
    initial_level,
    package_version,
    process_rows,
    resource_rows,
)
from supervision.logsink import LogLine
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

PACKAGE = Path(__file__).resolve().parents[1]


class FakeText:
    """Stands in for the Tk log pane: no tkinter, so the drain runs on Linux CI."""

    def __init__(self) -> None:
        self.lines: list[LogLine] = []

    def append_line(self, line: LogLine) -> None:
        self.lines.append(line)

    def line_count(self) -> int:
        return len(self.lines)

    def delete_oldest(self, count: int) -> None:
        del self.lines[:count]


def _lines(n: int, level: int = logging.INFO) -> deque[LogLine]:
    return deque(LogLine(level, f"m{i}") for i in range(n))


def test_takes_at_most_the_per_tick_cap_and_leaves_the_rest() -> None:
    lines, pane = _lines(250), FakeText()
    assert drain(lines, pane, 200, 2000, logging.DEBUG) == 200
    assert len(lines) == 50
    assert pane.lines[-1].text == "m199"


def test_trims_the_oldest_lines_from_the_pane() -> None:
    pane = FakeText()
    drain(_lines(40), pane, 100, 30, logging.DEBUG)
    assert [line.text for line in pane.lines] == [f"m{i}" for i in range(10, 40)]


def test_filtered_lines_are_drained_not_shown() -> None:
    lines = deque([LogLine(logging.DEBUG, "quiet"), LogLine(logging.ERROR, "loud")])
    pane = FakeText()
    assert drain(lines, pane, 200, 2000, logging.INFO) == 2
    assert not lines
    assert [line.text for line in pane.lines] == ["loud"]


def test_empty_deque_is_a_no_op() -> None:
    pane = FakeText()
    assert drain(deque(), pane, 200, 2000, logging.DEBUG) == 0
    assert pane.lines == []


# --- what the window says --------------------------------------------------------------------

T0 = datetime(2026, 10, 11, 18, 0, tzinfo=UTC)


def _snapshot(**fields: object) -> SupervisorSnapshot:
    base = SupervisorSnapshot(
        taken_at=T0, started_at=T0, phase=Phase.STARTING, hostname="mast07", role="unit", heartbeat_seconds=300.0
    )
    return SupervisorSnapshot.model_validate(base.model_dump() | fields)


def test_before_the_configuration_loads_the_header_says_unknown() -> None:
    top = header(_snapshot(), T0 + timedelta(seconds=5))

    assert (top.maintenance, top.opmode, top.degraded) == ("?", "?", "")
    assert top.summary.split("  ·  ") == ["mast07", "unit", "starting", "up 0:00:05", "config gen ?"]


def test_the_header_names_the_two_an_operator_acts_on() -> None:
    top = header(_snapshot(opmode=OpMode.CONTROLLED, in_maintenance=True, config_generation=418), T0)

    assert (top.maintenance, top.opmode) == ("true", "controlled")
    assert top.summary.endswith("config gen 418")


def test_the_uptime_follows_the_clock_between_publications() -> None:
    snapshot = _snapshot()

    assert "up 1:00:00" in header(snapshot, T0 + timedelta(hours=1)).summary


def test_a_degraded_configuration_is_shown_verbatim() -> None:
    assert (
        header(_snapshot(config_degraded_reason="boot cache from 2026-10-10"), T0).degraded == "boot cache from 2026-10-10"
    )


def test_rows_carry_their_severity() -> None:
    snapshot = _snapshot(
        resources=(ResourceStatus(name="share", state=ResourceState.DOWN, detail="Z: unreachable", since=T0),),
        processes=(
            ProcessStatus(name="vscode", mode=None, state=ProcessState.NOT_SUPERVISED, since=T0),
            ProcessStatus(
                name="pwi4", mode=SupervisionMode.SUPERVISED, state=ProcessState.CRASH_LOOPED, pid=5120, restarts=5, since=T0
            ),
        ),
    )

    [share] = resource_rows(snapshot)
    vscode, pwi4 = process_rows(snapshot)

    assert share == TableRow(("share", "down", "Z: unreachable", "18:00:00Z"), Severity.WARNING)
    assert vscode.values[1:4] == ("not supervised", "not_supervised", "?") and vscode.severity is Severity.OK
    assert pwi4.values[:5] == ("pwi4", "supervised", "crash_looped", "5120", "5") and pwi4.severity is Severity.ERROR


def test_about_carries_what_a_bug_report_needs() -> None:
    text = about_text(_snapshot(opmode=OpMode.OPERATED), "1.2.3", Path("/top"), None)

    for expected in (
        "mast-supervisor 1.2.3",
        f"top: {Path('/top')}",
        "host: mast07",
        "role: unit",
        "opmode: operated",
        "log: no daily log file",
    ):
        assert expected in text


def test_the_version_comes_from_pyproject() -> None:
    assert package_version() == tomllib.loads((PACKAGE / "pyproject.toml").read_text())["project"]["version"]


def test_every_filter_level_is_a_logging_level() -> None:
    assert all(isinstance(logging.getLevelName(level), int) for level in LEVELS)


def test_help_names_the_readme_and_the_log_folder() -> None:
    log = Path("/logs") / "2026-10-11" / "mast-unit-supervisor-log.txt"

    assert help_text(Path("/top/README.md"), log).splitlines() == [
        f"Documentation: {Path('/top/README.md')}",
        f"Logs: {log.parent}",
    ]
    assert help_text(Path("/r"), None).endswith("Logs: no daily log file")


def test_the_filter_starts_at_the_configured_level_or_info() -> None:
    assert initial_level("warning") == "WARNING"
    assert initial_level("verbose") == "INFO"
