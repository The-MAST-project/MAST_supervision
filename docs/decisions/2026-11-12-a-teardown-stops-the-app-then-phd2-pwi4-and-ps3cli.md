---
decided: 2026-11-12
status: proposed
issue: MAST_provisioning#82
areas:
  - process-supervision
  - services
  - status-reporting
---

# A teardown stops the app, then PHD2, PWI4 and ps3cli, and shuts a started-up app down first

**Why:** stage 5 installs the supervisor as a service, and the service's stop is the design's
"service stopping" flavor (`plans/supervisor-design.md` §7): stop the programs in order. Until
now the loop had only the restart flavor, which leaves everything running
(`2026-10-11-the-loop-matches-the-app-by-its-app-py-and-leaves-programs-running-on-stop`). Three
facts shaped the stop, all read in the code on 2026-11-12:

- `ManagedProcess` could stop a program only as the first half of a restart (`_stop`), so a
  stopped program came back after the backoff.
- The supervisor stops a program with `TerminateProcess`. For the unit app that skips
  `end_lifespan` and its `shutdown()`, so a started-up unit would be left with its covers open.
- `Unit.shutdown()` on a unit that was never started first connects its components, which
  enables the mount axes. A shutdown is therefore not safe to send unconditionally.

This is piece S1 of the vault's *MAST supervisor service plan*, whose recommendations were
approved tentatively on 2026-11-12 (T3 and D3 there).

**What:**

- **`ManagedProcess.stop(reason)` is a final stop.** It takes the restart's path (terminate, kill
  after `stop_grace_seconds`) through `_Stopping(final=True)` and ends in a `_Stopped` phase that
  does nothing more. The row ends `stopped` (a new `ProcessState`, at WARNING), or `blocked` when
  the program refuses terminate or survives kill; either way `stopped` is `True`, so a caller
  waiting on it is bounded. A stop during a restart turns the restart into a stop. A program not
  running is stopped at once and never spawned. It stops an observed program too: whether to ask
  is the caller's decision.
- **`Supervisor.run(stop, flavor)`** takes a `StopFlavor`: `LEAVE_RUNNING` (the default, as
  before) or `TEAR_DOWN`. Only a requested stop uses the flavor. A loop that dies on an exception
  leaves the programs running, since bouncing a tracking mount because of a supervisor bug is
  worse than leaving it up for the next supervisor to adopt.
- **`Supervisor.teardown()`** stops the rows named in `TEARDOWN_ORDER`, app, `phd2`, `pwi4`,
  `ps3cli`, each waited for before the next, ticking every row and publishing the snapshot while
  it waits. The app goes first so it lets go of PWI4 and PHD2 before they go. A row that is not
  there, or a `FixedRow` (a program that could not be started, or VSCode), is skipped.
- **PHD2 is stopped although it is observed.** It is the one exception to "observed is never
  killed": the app spawns it, and once the app is gone nothing would watch it.
- **The app's lifecycle state is structured.** `probe_app` puts a known `opstate` in
  `HealthResult.opstate`; `ManagedProcess` keeps the last one a healthy probe reported and puts it
  in `ProcessStatus.opstate`, cleared when the program exits. `OpState` is a local copy of
  `common.opmode.OpState`, pinned by `test_supervisor_state.py` like the other copies.
- **Shutdown first, only when started up.** When the app's last `opstate` is `startedup`, the
  teardown calls `Wiring.app_shutdown`: `probes.shutdown_app` sends `PUT .../unit/shutdown` and
  reads `status` until it reports `shutdown`, bounded by `APP_SHUTDOWN_WAIT_SECONDS` (200 s, above
  the unit's 120 s abort wait plus its 70 s settle bound). Then the app is stopped whatever the
  outcome. Under `initialized`, `shutdown` or no reported state, nothing is sent. Only a unit is
  wired with it; a spec app reports no `opstate`.
- **A watched app (`development`) is torn down too**, as the design's table says a service stop
  stops the app; its row shows `stopped` rather than `not_supervised` afterwards.

**Rejected:**

- *Send `shutdown` to every app before stopping it.* On a never-started unit it connects the
  components and enables the axes.
- *Stop all programs at once.* The app holds PWI4 and PHD2; stopping them under it would make its
  last moments fail against them.
- *Tear down when the loop dies.* See above.

**Unsettled:**

- Nothing calls `run` with `TEAR_DOWN` yet: the service's stop event is stage 5 (S3, S4).
- The teardown's length is bounded per row by twice `stop_grace_seconds`, plus up to 200 s for the
  app's shutdown, plus any probe the loop is waiting on (a stop waits for a running probe, up to
  70 s). NSSM's stop timeout has to cover the sum; it is set by the installer (S5) and is not
  measured yet.
- Whether a teardown on a `development` unit should stop the operator's app is a reading of the
  design table, not a separate decision. No `development` unit gets the service in the pilot.
- A `stopped` program cannot be started again without restarting the supervisor. Releasing it is
  the start half of the per-program control (S6).

**Implications:** S3 passes `TEAR_DOWN` on the service's stop event. The heartbeat, the window and
`GET /status` show `stopped` rows and carry each row's `opstate` with no further change.
