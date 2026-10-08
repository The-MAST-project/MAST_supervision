---
decided: 2026-10-08
status: accepted
areas:
  - process-supervision
  - status-reporting
---

# A copy in another login session blocks supervision and is never killed; a spawn gets a startup grace

**Why:** `plans/supervisor-design.md` §6 said: adopt same-session matches, and kill a
different-session orphan once, at startup. That rule was written when NSSM services ran PWI4 and
PHD2 in session 0, as LocalSystem: invisible, and with no `Z:`. MAST_provisioning#159 has since
removed every MAST service, so that copy no longer arises on a provisioned unit. What "another
session" still covers is an operator's Remote Desktop session, where a copy of PWI4 is usable and
may hold a connected, tracking mount. And the supervisor, running as `mast`, could not kill a
LocalSystem process anyway. Separately, §6 had no startup grace: with the default threshold of 3
and PWI4's 10 s interval, a spawned PWI4 had about 30 s to answer before being restarted, so a
slow cold start would loop into `crash_looped`.

**What:**

- **`managed.ManagedProcess` adopts any copy in its own login session as-is**, the oldest when
  there are several, and reports the others in the row's `detail` for as long as the adopted one
  runs. **A copy in another login session is never killed, never adopted, and no second copy is
  spawned beside it.** The row's state is the new `ProcessState.BLOCKED`, at ERROR severity, naming
  the pid and session. The supervisor looks again every probe interval and moves on once that copy
  exits: it spawns the program if supervised, or keeps probing it if observed. A process whose
  session cannot be read counts as another session's. **A respawn after the backoff looks
  first**, so a copy started during the backoff is adopted or blocks, never doubled. The notes
  about other copies are refreshed at every probe. Decided by Eli on 2026-10-08.
- **A program the supervisor cannot stop is `blocked` too.** A same-session copy started "as
  administrator" refuses `terminate` from the non-elevated supervisor (psutil `AccessDenied`,
  surfaced as `PermissionError`): the row goes `blocked` and no further stop is attempted. A
  process still running a grace after `kill` goes `blocked` once rather than being killed on
  every tick. Every terminate, kill, spawn and scheduled restart is logged.
- **A spawn gets a startup grace.** Failed probes do not count toward `unhealthy_threshold`
  until the first healthy probe or `STARTUP_GRACE_SECONDS = 120`, and the row is the new
  `ProcessState.STARTING` (WARNING) meanwhile. An adopted program gets no grace: it was already
  running. Decided by Eli on 2026-10-08.
- **`tick()` never blocks.** A probe runs on the program's own single-thread executor and is not
  started while the last one is still running. A restart is terminate, then kill once
  `stop_grace_seconds` has passed, then a respawn after the backoff, each step on a later tick. A
  tick follows the phase changes it causes (`MAX_STEPS_PER_TICK`), so a finished probe counts in
  the tick that started it.
- **The image looked for is `Path(argv[0]).name`**, compared ignoring case as Windows does, so
  discovery and spawning cannot disagree about the program.
- **A spawn runs in the executable's folder** (`Path(argv[0]).parent`), the working-directory fix
  MAST_unit#21 asks for. Decided by Eli on 2026-10-08, "for now": the long-term source of program
  locations is expected to be a registry left by provisioning, since where things are installed is
  provisioning's state, not configuration for the database.
- **`ProcessStatus.since`** is when the row entered its current state, as `ResourceStatus.since`
  already was.
- **`RestartPolicy` is a local dataclass** carrying the `ManagedProcessConfig` fields this module
  reads, plus `stop_grace_seconds`, so nothing here imports `common.config` (see the 2026-09-27
  enum-copy record). `Backoff` is a copy of common's private `_Backoff` (MAST_supervision#2 row 8).

**Rejected:**

- *Kill a different-session copy once* — the reasons above.
- *Adopt a different-session copy* — it may be invisible (session 0) or someone else's, and a
  row reporting it healthy would hide that the machine is not in the expected shape.
- *`UNHEALTHY` with a "starting" detail* — readers of `/status` could not tell starting from
  failing without parsing text.
- *A grace field in `ManagedProcessConfig`* — a MAST_common change for a value with no case for
  per-program tuning yet.

**Unsettled:**

- On Windows psutil's `terminate()` is `TerminateProcess`, the same as `kill()`, so the stop is
  not graceful. Closing a GUI program's main window first was not implemented.
- Programs are matched by image name only. A same-named program that is not ours would be adopted.
- **`CREATE_BREAKAWAY_FROM_JOB` is not passed yet.** Design §4 has the supervisor's children
  break away from the service's job object, so a service kill leaves PWI4 running. It belongs with
  that job object in stage 5: passed now, it fails the spawn under any parent job that disallows
  breakaway, which a stage 3 run started from a terminal may be in.
- `OsProcessApi` is untested here: it is Windows-only (`process_session_id`), and the Linux CI
  leg cannot exercise it.
- A probe still running when its process is restarted finishes on the executor, and the new
  process's first probe waits behind it, for at most that probe's timeout.

**Implications:** stage 3 maps `ManagedProcessConfig` to `RestartPolicy` and drives `tick()`
from the loop. MAST_supervision#2 row 5 (executable locations) points at a provisioning-left
location registry rather than at a MAST_common locator.
