---
decided: 2026-11-17
status: proposed
issue: MAST_provisioning#82
areas:
  - process-supervision
  - services
---

# A hold or a teardown shuts down an app whose covers may be open, read fresh at stop time

**Why:** two paths stopped a unit app that could have its covers open without sending it `shutdown` first, both found reading the merged stage 5 code on 2026-11-17:

- **A hold.** `PUT /programs/app/stop` on 8005 stopped the app's row directly. Only the teardown sent a started-up app its shutdown first (`2026-11-12-a-teardown-stops-the-app-then-phd2-pwi4-and-ps3cli`), so a hold at night left a working unit with its covers open and no app.
- **A lifecycle change under way.** The teardown sent the shutdown only when the row's last `opstate` was `startedup`. A unit's `opstate` changes only when a startup or a shutdown ends (MAST_unit `Unit.ontimer`, `do_shutdown`). A startup reports the opstate it began from (`initialized`, or on a working night usually `shutdown` after an idle auto-shutdown) until it is done, and a shutdown reports `shutdown` while its covers are still closing. So `sc stop mast-service` during either terminated the app partway.

Reading the code also showed that `shutdown_app` counted a shutdown done on the first status read showing `shutdown`, which a unit shut down earlier shows from the start, and that the row's `opstate` comes from the last probe, up to a probe interval old.

**What:**

- **One rule, in `probes.py`:** `may_have_covers_open` is true when the app is `startedup`, or its own `activities_verbal` lists `StartingUp` or `ShuttingDown`. An app idle in `initialized` or `shutdown` still gets no shutdown, since `shutdown` on a never-started unit connects its components and enables the axes (team decision, 2026-11-12, tentative). A unit takes a `shutdown` during a startup (the startup then completes into `shutdown`), and a second one during a shutdown is a no-op.
- **One entry point, `shut_down_if_covers_may_be_open(port, role)`,** reads the status fresh at stop time, applies the rule, and when it holds sends the shutdown and waits. It is `Wiring.app_shutdown` for a unit, used by both the teardown and the hold. Its answer is `None` when no shutdown was due.
- **A shutdown is done** at `opstate` `shutdown` with no `ShuttingDown` left among the activities. The unit's `activities_verbal` is `null` when idle, which reads as no activities.
- **A hold runs the check off the loop.** It can take `APP_STATUS_TIMEOUT_SECONDS` plus `APP_SHUTDOWN_WAIT_SECONDS`, and every row, the resource probes and the published snapshot wait on the loop's thread. It runs on a single worker thread (`Wiring.submit_shutdown` in tests). The row is stopped on the first tick after the check ends, whatever it found. The program is listed under `held` from the request on.
- **During the check:** a second stop request is a no-op. A start (release) drops the pending stop; a shutdown already sent goes on, so the app is left running, shut down. A teardown drops it too, and runs its own check.

**Implications:**

- A hold of the app returns `202` at once, as before, but its row reads `stopped` only once the check ends: at once for an idle app, up to about 270 s for one that is shut down first. An app that exits by itself meanwhile is restarted by the row, then stopped when the check ends.
- The teardown's check still runs on the loop, which is ending then.
- The rule covers a unit started up by an autostartup route (MAST_unit#395) as well.
- A hold applies under a console (`--no-service`) supervisor too: the action listener serves the routes for both launches.
