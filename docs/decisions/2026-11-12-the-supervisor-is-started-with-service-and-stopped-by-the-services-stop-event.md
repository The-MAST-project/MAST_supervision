---
decided: 2026-11-12
status: proposed
issue: MAST_provisioning#82
areas:
  - process-supervision
---

# The supervisor is started with `--service` and stopped by the service's stop event

**Why:** `__main__` refused to run without `--no-service` "until the service exists". Stage 5 needs the supervisor to accept the launch `mast-service` gives it (`service.py`, piece S4 of the supervisor service plan in the vault), to stop when the service asks, and to stop in the way a service stop means: the machine is being taken down, so the programs go too (the teardown of `2026-11-12-a-teardown-stops-the-app-then-phd2-pwi4-and-ps3cli`). A developer's console run must keep today's behavior.

**What:**

- **`--service` beside `--no-service`**, mutually exclusive; exactly one of `--dry-run`, `--service` and `--no-service` is given, and `--dry-run` takes no other option (it starts nothing). `parse_args` names the launch as `Launch.SERVICE`, `Launch.CONSOLE` or none for a dry run; anything else is a usage error (exit 2) saying what to give.
- **Under `--service` the stop event is opened first**, before the ports are claimed. `open_stop_event` opens `Global\mast-supervisor-stop` with `SYNCHRONIZE` only, which is all its DACL gives interactive users. When it cannot be opened, the supervisor logs why and exits 1: a `--service` supervisor that nothing can stop does not start, and the message says to run by hand with `--no-service`.
- **A daemon thread, `watch_stop_event`, waits on the event** in one-second waits and sets the loop's stop when it is signaled. It returns once the loop's stop is set either way.
- **The stop flavor follows the launch** (`stop_flavor`): `SERVICE` tears down, `CONSOLE` leaves the programs running. Ctrl-C under `--no-service` sets the same stop and leaves the programs running, as before; a loop that dies leaves them running under either launch (unchanged from S1).
- **Children break away from the job under the service only** (`spawn_flags`, `OsProcessApi(launch)`): `CREATE_NO_WINDOW | CREATE_BREAKAWAY_FROM_JOB` under `SERVICE`, `CREATE_NO_WINDOW` under `CONSOLE`, and no flags off Windows, where `Popen` refuses any. The design's §4 adds the breakaway flag always (D4 in the plan departs from it).
- **The window's close hint follows the launch** (`close_hint`): under the service it names `sc stop mast-service`, which stops the programs too; under the console it keeps "Ctrl-C in its console". It travels to the window in `WindowContext.close_hint`.

**Rejected:**

- *Always add `CREATE_BREAKAWAY_FROM_JOB`*, as the design's §4 sketches. A developer running `--no-service` from a terminal that holds its children in a job without `BREAKAWAY_OK` would have every spawn fail. *Inference* from the Win32 documentation, not tested on a machine.
- *Signal the loop by Ctrl-C or a console control event from the service.* The supervisor runs windowless in another session; there is no console to send to, and the event with its DACL is what S2 built for this.
- *Keep `--no-service` meaning "required" and make the service launch the default.* A bare `python -m supervision` typed by hand would then claim to be the service and fail on the missing event; naming the launch keeps the mistake impossible.

**Unsettled:**

- Whether the stop thread's one-second wait is the right granularity: it bounds how late a stop is noticed, not how long the teardown takes, which S1's waits and NSSM's stop timeout (piece S5) decide.
- The real open of the event runs only on CI's Windows leg and the dev VM; the service that creates it is S4.

**Implications:** S4's `service.py` starts `python -m supervision --service` and signals `Global\mast-supervisor-stop` on a service stop. The dev-VM acceptance (items 2, 9 and 18 of the plan) checks the teardown order, the breakaway, and that `--no-service` from a VS Code terminal still starts programs.
