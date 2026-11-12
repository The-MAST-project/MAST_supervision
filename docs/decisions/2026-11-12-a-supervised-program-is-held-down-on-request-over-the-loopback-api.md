---
decided: 2026-11-12
status: proposed
issue: MAST_provisioning#82
areas:
  - process-supervision
  - status-reporting
---

# A supervised program is held down on request over the loopback API, and released the same way

**Why:** under the service (stage 5), killing a program no longer stops it: the supervisor sees it gone and starts it again. Without a way to hold one program down, an engineer who needs PWI4 or ps3cli off, or wants to run the unit app by hand, has only `sc stop mast-service`, which tears the whole stack down. Arie asked for start, stop and probe per resource on 2026-11-11, with a new endpoint on the localhost-bound API (the supervisor service plan in the vault, decision T1, tentatively approved 2026-11-12, puts this before the pilot).

**What:**

- `PUT /mast/api/v1/supervisor/programs/{name}/{action}`, `action` being `stop` or `start` (`ProgramAction` in `state.py`), served by `build_action_app` on the `127.0.0.1:8005` listener only; the `0.0.0.0:8004` status listener does not serve it (`api.py`, `PROGRAM_ACTION_PATH`). `requested_by` is a query parameter, as on `PUT /ramdisk/restart`.
- `Supervisor.request_program` checks the request against the **published snapshot** and queues it on a `queue.SimpleQueue`; `tick()` applies the queue first (`_apply_requests`). So no request thread touches a row, and the answer never waits on the loop. Answers (`ControlAnswer`): `accepted` (`202`), `unknown` (`404`: no row of that name, which includes every name before the configuration loads), `not_supervised` (`409`).
- **Only a `supervised` row can be held.** The check is the row's `mode`, not a list of names, so it follows the configuration: PHD2 is `observed` (the app spawns it until stage 6), VSCode and a `development` app are not supervised, and all three are refused. A `production` app, PWI4 and ps3cli can be held.
- A stop is `ManagedProcess.stop` (S1's final stop): terminate, kill after the grace, never respawn. The name goes into `SupervisorSnapshot.held`, and the row reads `stopped`, with `held: stop requested by <who>` as its detail.
- A start is the new `ManagedProcess.release`: a stopped row goes back to looking for the program, and starts it if supervised; one still being stopped is let finish dying and is then restarted after the backoff, as an ordinary restart. A start on a running program does nothing.
- A hold lives in the supervisor's memory only (decision T8): a supervisor that restarts starts every program again. The README says so.

**Rejected:**

- *Answer from inside the loop*, with the request thread waiting for the next tick. It would hold an HTTP request for up to a tick and a slow resource probe, for an outcome `/status` already reports.
- *Refuse by name* (`phd2` is never holdable). The configuration already says which programs are supervised; a second list would drift from it.
- *Persist holds* (in the configuration database or a file). A hold that outlives the supervisor is a program nobody remembers stopping. Not for the pilot.

**Unsettled:**

- A hold changes the row to `stopped`, a WARNING (S1's choice), so the snapshot's severity is WARNING while anything is held. Whether a deliberate hold should count against the machine's severity is not decided.
- The window shows the held row like any `stopped` row; buttons and a traffic light are piece S7.
- A held program survives a teardown as it is: the teardown's stop of an already stopped row does nothing.

**Implications:** the window's per-row controls (S7) call the same `request_program`. Nothing here is reachable from another machine; a remote "stop" would need its own decision.
