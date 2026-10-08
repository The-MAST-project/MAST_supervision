---
decided: 2026-10-08
status: accepted
areas:
  - status-reporting
  - process-supervision
---

# Process probes report health, not state, with timeouts no stricter than the app's own clients

**Why:** Stage 2 part 3 adds the health probes for PWI4, PHD2, ps3cli and the app
(`plans/supervisor-design.md` §6). Part 4 will restart a supervised program after
`unhealthy_threshold` failed probes, so a probe that times out sooner than the program
legitimately answers turns a slow night into a restart. The design's table gave the checks but
no timeouts. The app probe was first proposed at 5 s; Eli doubted it. No log records how long
`/unit/status` takes: the unit log has no access lines, MAST-control's log stamps only a poll's
completion, and no unit was running the app on 2026-10-08 to time one. What the code shows is
that `Unit.status()` reads its components in sequence on the request thread, PHD2 twice over RPC
(`DEFAULT_RPC_TIMEOUT = 30.0` each) and PWI4 several times (`timeout_seconds = 10` each).

**What:** `probes.py` holds `probe_pwi4`, `probe_phd2`, `probe_ps3cli` and `probe_app`, each
returning `HealthResult(healthy, detail)`. Thresholds, restarts and `ProcessState` stay in
`managed.py`. A probe turns the failures it expects (refused, timed out, an unreadable answer)
into `healthy=False`; anything else propagates, as in `ResourceTracker`.

- **Timeouts match the clients:** PWI4 10 s, PHD2 30 s, and the app
  `APP_STATUS_TIMEOUT_SECONDS = 70`, above two back-to-back PHD2 RPCs. The TCP connect timeout
  is 3 s, because on the units a closed local port is refused only after about 2 s (measured
  2026-10-08 with curl on mast01-mast08: 2.01-2.06 s); a shorter one reports "nothing
  listening" as "no answer".
- **PHD2 is one instance on the fixed port `PHD2_PORT = 4400`**, decided by Eli on 2026-10-08,
  not the `4400 + instance - 1` that `unit.phd2` computes.
- **The app probe reports `opstate` in `detail`** when the status carries one (MAST_unit#297
  added it to `FullUnitStatus`), for display. Health is "200 with a `value`" only; neither
  `operational` nor `opstate` makes the app unhealthy, as §8 requires. Decided by Eli on
  2026-10-08.
- **HTTP goes through an opener with `ProxyHandler({})`**, because the units carry
  `http_proxy` and `urllib` would otherwise send a `127.0.0.1` request through it.
- **The app's port is a parameter.** It comes from the `services` collection, which the
  caller reads once config has loaded; `probes.py` reads no configuration.

**Rejected:**

- *A 5 s app timeout* — shorter than a status reply the app itself considers normal.
- *Importing `pwi4_client` or `unit.phd2`* — §6: `PWI4Status` raises `KeyError` on a 200 that
  is not PWI4, and the PHD2 connector is the spawner the supervisor replaces.

**Unsettled:** 70 s is derived from the client timeouts, not measured; the first night the app
runs, `/unit/status` should be timed on a unit and the bound revisited. `urllib`'s timeout
bounds each socket operation, not the whole request, so a server trickling bytes can outlast
it. With a 70 s app probe on a 15 s interval and a 30 s PHD2 probe on 15 s, part 4's runner
must not start a probe while the previous one is still running.

**Implications:** MAST_supervision#2 gains the PHD2 app states as a copied value, and its PHD2
port row becomes the fixed 4400.
