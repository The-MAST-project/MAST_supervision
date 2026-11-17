---
decided: 2026-11-17
status: proposed
issue: MAST_supervision#31
areas:
  - process-supervision
  - services
---

# A hold or a teardown shuts down an app whose covers may be open, read fresh at stop time

**Why:** two paths (MAST_supervision#31 and #32) stopped a unit app that could have its covers open without sending it `shutdown` first, both found reading the merged stage 5 code on 2026-11-17:

- **A hold.** `PUT /programs/app/stop` on 8005 stopped the app's row directly. Only the teardown sent a started-up app its shutdown first (`2026-11-12-a-teardown-stops-the-app-then-phd2-pwi4-and-ps3cli`), so a hold at night left a working unit with its covers open and no app.
- **A lifecycle change under way.** The teardown sent the shutdown only when the row's last `opstate` was `startedup`. A unit's `opstate` changes only when a startup or a shutdown ends (MAST_unit `Unit.ontimer`, `do_shutdown`). A startup reports the opstate it began from (`initialized`, or on a working night usually `shutdown` after an idle auto-shutdown) until it is done, and a shutdown reports `shutdown` while its covers are still closing. So `sc stop mast-service` during either terminated the app partway.

Reading the code also showed that `shutdown_app` counted a shutdown done on the first status read showing `shutdown`, which a unit shut down earlier shows from the start, and that the row's `opstate` comes from the last probe, up to a probe interval old.

**What:**

- **One rule, in `probes.py`:** `may_have_covers_open` is true when the app is `startedup`, or its own `activities_verbal` lists `StartingUp` or `ShuttingDown`. An app idle in `initialized` or `shutdown` still gets no shutdown, since `shutdown` on a never-started unit connects its components and enables the axes (team decision, 2026-11-12, tentative). A unit takes a `shutdown` during a startup (the startup then completes into `shutdown`), and a second one during a shutdown is a no-op.
- **One entry point, `shut_down_if_covers_may_be_open(port, role, last_opstate)`,** reads the status fresh at stop time, applies the rule, and when it holds sends the shutdown and waits. It is `Wiring.app_shutdown` for a unit, used by both the teardown and the hold. Its answer is `None` when no shutdown was due.
- **An unreadable status falls back on the row's last probed opstate:** when that was `startedup`, the shutdown is sent anyway. A status read waits on PHD2's RPC and can time out while `shutdown`, which only starts a thread, is still taken.
- **Only an app with a process is checked.** An app already stopped (after a hold), never launched, or crash-looped is sent nothing, so the warning for an unconfirmed shutdown keeps meaning the covers may be open.
- **A shutdown is done** at `opstate` `shutdown` with no `ShuttingDown` left among the activities. The unit's `activities_verbal` is `null` when idle, which reads as no activities.
- **A hold runs the check off the loop.** It can take `APP_STATUS_TIMEOUT_SECONDS` plus `APP_SHUTDOWN_WAIT_SECONDS`, and every row, the resource probes and the published snapshot wait on the loop's thread. It runs on a daemon thread (`Wiring.submit_shutdown` in tests): a pool's workers are joined at exit, so a console supervisor stopped during a hold would wait out the shutdown first. The row is stopped on the first tick after the check ends, whatever it found. The program is listed under `held` from the request on.
- **During the check:** a second stop request is a no-op. A start (release) drops the pending stop; a shutdown already sent goes on, so the app is left running, shut down. A teardown runs its own check and stops the app; the hold's check is left to finish on its thread.
- **The service's stop grace grows by one status read:** `SUPERVISOR_STOP_GRACE_SECONDS` 380 → 450 s (`service.py`, `APP_STATUS_READS`), and NSSM's console timeout 420 → 490 s.

**Rejected:**

- **Deciding on the row's last opstate alone,** as before: it is up to a probe interval (15 s) plus a probe (up to 70 s) old, so a startup sent just before the stop is missed. It is kept as the fallback only.
- **A pre-stop phase in `ManagedProcess`** serving both paths, which would also let the teardown publish while it waits: the larger change, for a loop that is ending anyway.
- **Running the hold's check on the probe executor:** it has one thread, so a 70 s probe in flight would hold the shutdown up, and `Submit` is typed for probes.

**Unsettled:**

- The worst case of a shutdown during a startup whose dispatch stalls is 60 + 120 + 70 = 250 s on the unit side, above `APP_SHUTDOWN_WAIT_SECONDS` (200 s); the stall is understood to take seconds in practice. Not measured.
- Read from the code only: the unit's `activities_verbal` while a startup or a shutdown runs. The dev-VM acceptance is the first run.

**Implications:**

- A hold of the app returns `202` at once, as before, but its row reads `stopped` only once the check ends: at once for an idle app, up to about 270 s for one that is shut down first. An app that exits by itself meanwhile is restarted by the row, then stopped when the check ends.
- The teardown's check still runs on the loop, which is ending then.
- The rule covers a unit started up by an autostartup route (MAST_unit#395) as well.
- A hold applies under a console (`--no-service`) supervisor too: the action listener serves the routes for both launches.
