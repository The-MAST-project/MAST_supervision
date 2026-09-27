---
decided: 2026-09-27
status: accepted
areas:
  - status-reporting
---

# A snapshot's severity is its worst state, and `not_supervised` is never a fault

**Why:** Three surfaces render the supervisor's state from one snapshot (`state.py`): the
window, the heartbeat block in the log, and `GET /status`. Each needs to know how bad things
are, and the plan fixes the answer in two places — the heartbeat logs at INFO when all is
green, WARNING when a resource is short or a process unhealthy, ERROR when anything is
crash-looped (`plans/supervisor-design.md` §7); and VSCode under `automatic` is launched
once and never watched, so its absence must not read as a fault (§6, §8; decided with Arie
in the 2026-09-22 round). Left to each renderer, the rule would be written three times and
drift, and the first surface to forget the VSCode exception would red out every developer
machine on any fleet view built over `/status`.

**What:** `ProcessState` carries `NOT_SUPERVISED` beside `HEALTHY`, `UNHEALTHY` and
`CRASH_LOOPED`. Each `ResourceState` and `ProcessState` member maps to a `Severity`, whose
values are the logging levels `INFO` / `WARNING` / `ERROR`, and
`SupervisorSnapshot.severity` is the maximum over every resource and process — a
`computed_field`, so `/status` serializes it. `NOT_SUPERVISED` maps to `Severity.OK`, as does
an empty snapshot. A `DEGRADED` resource and a `DOWN` one are both `WARNING`: §5 makes no
resource fatal, so the supervisor proceeds either way.

`ProcessStatus.mode` is `SupervisionMode | None`, and `None` goes with `NOT_SUPERVISED` and
nothing else: none of `supervised` / `observed` / `disabled` describes a program launched
once and never watched, so the row would otherwise report a mode it is not in. A validator
also rejects the other contradictions — a `disabled` program reported at all, and an
`observed` one `CRASH_LOOPED`, since observation never restarts.

**Rejected:**

- *A severity rule in each renderer* — for the drift under Why.
- *`NOT_SUPERVISED` as a flavor of `UNHEALTHY` with a flag* — every consumer would have to
  know the flag to avoid the false fault, which is the failure the state exists to prevent.
- *Dropping `mode` from `ProcessStatus`* — the window and `/status` would lose the
  observed-versus-supervised distinction that explains why PHD2 is not restarted.
- *`DOWN` as `ERROR`* — ERROR is reserved for the one condition the supervisor has stopped
  acting on (`CRASH_LOOPED`), so `grep ERROR` over a night's log finds the nights that need
  a person.

**Unsettled:** `Phase` does not contribute to severity. `DEGRADED` as a phase follows from a
short resource, which already counts; whether a supervisor stuck in `WAITING_FOR_RESOURCES`
past its budget deserves more than WARNING was not considered.

**Implications:** The heartbeat (stage 3) logs at `snapshot.severity` directly, and the GUI's
colors key on it; neither implements the rule.
