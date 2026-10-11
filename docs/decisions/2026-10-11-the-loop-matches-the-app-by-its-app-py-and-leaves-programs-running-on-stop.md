---
decided: 2026-10-11
status: accepted
areas:
  - process-supervision
  - status-reporting
---

# The loop matches the app by its `app.py`, watches an operator's app as not supervised, and leaves the programs running in hidden consoles when it stops

**Why:** stage 3 part 1 joins stage 2's parts into a loop that can run on mast00 in a console
(`plans/supervisor-design.md` §5-§8, §15). Joining them raised questions the stage 2 parts did not:

- `ManagedProcess` found programs by image name. The app's image is `python.exe`, the same as
  the supervisor's own and anything else run from the venv.
- Under `operated` the app is started by the operator. An observed `ManagedProcess` reports a
  program that is not running as `unhealthy`, which would make every heartbeat a WARNING through
  the day.
- Without the service there is one way to stop. The design's two shutdown flavors (§7) had no
  console form.

**What:**

- **The app is matched by its `app.py` on the command line**, compared ignoring case and slash
  direction: `ProcessApi.find(image, argument)`, through `names_argument`. A process whose
  command line cannot be read does not match. This applies under `controlled`, where the app is
  supervised, and under `operated`, where it is watched. Decided by Eli, 2026-10-11.
- **Of a launcher and the copy it launched, only the launched one counts.** debugpy's launcher
  and a Windows venv's redirecting `python.exe` start the real interpreter with the same
  arguments, so a parent and its child both match. `FoundProcess.parent` lets `ManagedProcess`
  drop a match that is the parent of another match, which would otherwise be adopted as the
  oldest and reported as a duplicate. This applies to every program, not only the app.
- **`ProcessApi.spawn` takes the environment**, and `ManagedProcess` takes optional `cwd` and `env`.
  The app runs from its `src/` with the launcher's environment. Other programs keep their
  executable's folder and inherit.
- **An app the operator starts is `not_supervised` while absent.** `WatchedApp` wraps an observed
  `ManagedProcess`. While the app runs, its row is the probe's verdict (`observed`). While it does
  not, the row is `not_supervised`, unless a copy in another session blocks it.
- **`--no-app` launches neither VSCode nor the app** and watches the app as under `operated`. That
  is the mast00 night's shape: the supervisor owns PWI4 and ps3cli, and the operator runs VSCode as
  today.
- **Stopping leaves the programs running** (the restart flavor). Nothing is terminated, and the
  next start adopts what it finds. The ordered teardown waits for the service in stage 5. Decided
  by Eli, 2026-10-11.
- **A console child gets a hidden console of its own** (`CREATE_NO_WINDOW`, `managed.SPAWN_FLAGS`),
  as MAST_common's `process.py` already starts them. A child sharing the supervisor's console
  would get its Ctrl-C and be killed when its window closes, which would break the previous
  point for ps3cli and a `controlled` app. The app's output goes only to its daily log file.
  Decided by Eli, 2026-10-11, over a visible console per child.
- **The configuration is watched** (`Config.start_watching()`, as the unit app does). Its
  generation, its degraded reason and the maintenance flag are re-read on every resource poll,
  so a supervisor that came up on the boot cache leaves `DEGRADED` once the database answers.
  `opmode` and the process list are read once; changing them is restart-to-apply (§7).
  `--dry-run` does not start the watcher.
- **The resource wait uses `ResourcesConfig`'s defaults**, because it runs before the
  configuration can be read. A configured index set or budget therefore applies only after a
  restart, and today it is not applied at all.
- **A stale snapshot means the loop stopped only past a margin.** The snapshot is republished on
  change and on every heartbeat. A tick can take seconds when the network is down, because the
  resource probes and the configuration load run on the loop thread, each bounded. So the margin
  is a `taken_at` older than the heartbeat interval plus about ten seconds, not the interval alone.
- **`DEGRADED` means a resource short of `ok`, or a configuration served from the boot cache.**
  Process health is not part of the phase; it shows in the rows and in the snapshot's severity.
- **`programs.py` is the one table of what a program name means**: image, locator, default
  arguments, probe. The loop and `--dry-run` both resolve from it. A program that cannot be
  started is a fixed `unhealthy` row naming why, so a missing install shows instead of crashing.
  A configured `exe` takes the configured `args` only.
- **The heartbeat interval before the configuration loads** is `SupervisorConfig`'s default.

**Rejected:**

- *Match the app by working directory.* Under debugpy the process's working directory is
  VSCode's choice, not the app's.
- *Report an operator's absent app as `unhealthy`.* A heartbeat that is a WARNING all day
  teaches the reader to ignore WARNING.
- *Tear down in order on Ctrl-C.* A dropped console would take down a tracking mount.

**Unsettled:**

- `ManagedProcess`'s probe executors are not daemon threads, so a stop waits for a running
  app probe, up to 70 s.
- `operated` launches VSCode once per supervisor start (`--no-app` launches nothing). After a
  restart-to-apply, that may open a second window or focus the existing one, depending on
  VSCode. Not checked.
- Matching relies on the app's path appearing verbatim as one argument. A launcher that passes
  a relative path or an 8.3 short name would not match. Not seen on the units.
- `wiring.load_config` and `wiring.config_status` run only against a real configuration and
  are untested here. `OsProcessApi.find`'s psutil iteration and parent lookup are Windows-side
  and untested. `names_argument` and the spawn flag are tested.

**Implications:** part 2 serves `Supervisor.snapshot` from `GET /status`. The mast00 night runs
`--no-gui --no-service --no-app` after `--dry-run`.
