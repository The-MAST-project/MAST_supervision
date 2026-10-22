"""The heartbeat: the supervisor's whole state as one log record, every few minutes and on each phase change.

Transitions are logged once, so a quiet log alone cannot tell "nothing changed" from "the
supervisor died at 21:00". The heartbeat is what tells them apart, and what is read the morning
after. It is rendered from a published snapshot, never from live state.

- **One record**, multi-line, so the formatter prefixes it once and the block stays together.
- **ASCII only**: the daily file dropped a U+2033 that the console showed (MAST_common#63).
  Details come from probes and error messages, so anything else is replaced, not trusted.
- **Logged at the snapshot's severity**, so ``grep ERROR`` over a night's file finds the bad nights.

Design: mast-claude-config ``plans/supervisor-design.md`` §7.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from supervision.state import ProcessState, RamdiskRevival, ResourceState, RevivalState, SupervisorSnapshot

UNKNOWN = "?"
NOT_YET_PROBED = "not yet probed"
NO_PROCESSES_YET = "none yet"
QUIET_PROCESS_STATES = frozenset({ProcessState.HEALTHY, ProcessState.NOT_SUPERVISED})


def render_heartbeat(snapshot: SupervisorSnapshot) -> tuple[int, str]:
    """The logging level and the text of the heartbeat for ``snapshot``."""
    lines = [
        f"supervisor heartbeat  up {format_uptime(snapshot.uptime)}  phase={snapshot.phase}",
        f"  role={snapshot.role}  opmode={or_unknown(snapshot.opmode)}  "
        f"maintenance={or_unknown(snapshot.in_maintenance)}  config gen={or_unknown(snapshot.config_generation)}",
    ]
    if snapshot.config_degraded_reason:
        lines.append(f"  config degraded: {snapshot.config_degraded_reason}")
    resources = "  ".join(
        f"{r.name}={r.state}" + ("" if r.state is ResourceState.OK else f" ({r.detail})") for r in snapshot.resources
    )
    lines.append(f"  resources  {resources or NOT_YET_PROBED}")
    processes = [
        f"{p.name}={p.state}"
        + (f" pid={p.pid}" if p.pid is not None else "")
        + f" r={p.restarts}"
        + ("" if p.state in QUIET_PROCESS_STATES else f" ({p.detail})")
        for p in snapshot.processes
    ]
    lines.append(f"  processes  {'  '.join(processes) or NO_PROCESSES_YET}")
    if snapshot.ramdisk_revival is not None:
        lines.append(f"  ramdisk    {render_revival(snapshot.ramdisk_revival)}")
    text = "\n".join(lines).encode("ascii", "replace").decode("ascii")
    return snapshot.severity, text


def render_revival(revival: RamdiskRevival) -> str:
    """E.g. ``lost 02:14, restored 02:15 (requested by unit)``, in UTC like the snapshot's timestamps."""
    text = f"lost {format_clock(revival.lost_at)}"
    if revival.restored_at is not None:
        text += f", restored {format_clock(revival.restored_at)}"
    if revival.requested_by:
        text += f" (requested by {revival.requested_by})"
    text += f"  state={revival.state}"
    if revival.state is not RevivalState.IDLE and revival.detail:
        text += f" ({revival.detail})"
    return text


def format_clock(moment: datetime | None) -> str:
    return UNKNOWN if moment is None else moment.strftime("%H:%M")


def format_uptime(uptime: timedelta) -> str:
    return str(timedelta(seconds=int(uptime.total_seconds())))


def or_unknown(value: object) -> str:
    match value:
        case None:
            return UNKNOWN
        case bool():
            return str(value).lower()
        case _:
            return str(value)
