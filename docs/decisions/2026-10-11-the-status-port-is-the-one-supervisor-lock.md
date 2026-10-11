---
decided: 2026-10-11
status: accepted
areas:
  - status-reporting
  - process-supervision
---

# The status port is the one-supervisor lock: a live holder aborts the new supervisor, a dead one is killed and taken over

**Why:** stage 3 part 2 serves `GET /status` on port 8004 (`plans/supervisor-design.md` §7).
The design applied the window's rule to the API: a bind failure degrades to no API with one
WARNING, because *the supervisor must never die because a surface could not open*. That rule
was found to be taken too literally. The likeliest reason the port is taken is an earlier
supervisor still running, mid-shutdown or wedged. Degrading would leave a supervisor with no
API running beside one that does answer: two loops adopting and restarting the same programs,
with the one nobody can see in charge. Failing the start was judged better. The rule still
holds for the window: a window that cannot open, or headless operation, never stops the
supervisor. Team decision, 2026-10-11.

**What:**

- **The port is claimed first.** `__main__.run` calls `instance.claim_port` before
  `Supervisor.run`, so a supervisor that cannot have the port never reaches its programs.
  `--dry-run` claims nothing. `claim_port` binds the socket itself (`bind_listening`) and
  hands it to `api.StatusServer`, so a failed bind is a decision here and never uvicorn's
  `sys.exit` on its thread. Listening starts at the bind, so an early request waits in the
  backlog.
- **When the port is taken, the holder is asked for `/status`** (`fetch_liveness`, no proxy) and
  classified (`classify`). Only `taken_at`, `heartbeat_seconds` and `phase` are read
  (`Liveness`), so a running supervisor of an earlier or later snapshot schema is still judged
  live rather than killed for failing to validate:
  - **live**, a fresh snapshot in any phase: this supervisor logs an ERROR and exits 1. A
    `DEGRADED` or `waiting_for_resources` peer is reporting a problem a replacement would meet
    too, so health does not enter into it, only liveness.
  - **`stopping`**: waited for up to `Timings.stopping_wait_seconds`, then taken over as stuck.
    A supervisor closes its listener right after publishing `STOPPING` (`__main__.run` stops
    the server before the probe executors are joined), so the wait is three times
    `api.SERVER_STOP_SECONDS`, 15 s.
  - **dead**: no answer, a stale snapshot, or a reply that is not a snapshot.
- **The holder is looked up before and after the probe**, as an `Owner` with its pid and
  creation time, and only a holder that stayed the same process is killed. One that changed,
  such as another supervisor that took over meanwhile, is probed afresh; one that is gone is a
  port to bind again, for up to `rebind_seconds`, so a normal handoff never aborts.
- **Taking over** uses the listening process (`PsutilPortOwners.owner`). If it runs
  `-m supervision` (`runs_module`), it is terminated, killed after a grace, and the port
  rebound with a short retry; its programs keep running and are adopted as on any start. A
  holder that is not a supervisor, a port nobody listens on, a lookup or kill that is denied,
  or any other bind failure: ERROR, exit 1.
- **A predecessor in another login session is killed like one in this session.** The
  2026-10-08 rule that another session's copy is never killed
  (`2026-10-08-a-copy-in-another-login-session-blocks-and-is-never-killed.md`) covers the
  programs a supervisor runs, not another supervisor. Team decision, 2026-10-11.
- **`Phase.STOPPING`** is published when `Supervisor.run` ends, by the stop event or by
  Ctrl-C, so a supervisor starting meanwhile waits instead of aborting.
- **The snapshot carries `heartbeat_seconds`**, the interval in force, and
  `SupervisorSnapshot.is_stale` judges a `taken_at` older than it plus `STALE_MARGIN_SECONDS`
  (10 s, the longest tick). A configured interval longer than the default would otherwise make
  a live peer look dead and be killed. Fleet pollers get the same rule.
- **Windows binds with `SO_EXCLUSIVEADDRUSE`**, POSIX with `SO_REUSEADDR`. On Windows
  `SO_REUSEADDR` would let a second socket bind the port, which is what the lock must refuse;
  on POSIX it only allows rebinding over a killed holder's `TIME_WAIT` connections.
- **The route** is `GET {Const.BASE_SUPERVISOR_PATH}/status`, returning
  `CanonicalResponse(value=snapshot.model_dump(mode="json"))`, read-only, with the access log
  off and uvicorn's loggers propagating (`log_config=None`).

**Rejected:**

- *Degrade to no API on a failed bind*, as the design first said: it hides the split-brain the
  taken port most likely means.
- *Abort only on a healthy peer, and replace a degraded one.* The replacement would meet the
  same network or RAM-disk problem, and killing a working loop gains nothing.
- *Kill whatever holds the port.* An unknown program on 8004 is a configuration problem to
  report, not something to kill.
- *A lock file or named mutex beside the port.* Two locks can disagree; the port is the thing
  that must be exclusive anyway.
- *FastAPI's `TestClient` in the tests*: it needs httpx, which this repo does not declare, and
  is deprecated with it. The tests run the real server on an ephemeral port.

**Checked on Windows** (labcomp2, 2026-10-11, Python 3.12.10, psutil 7.2.2, real processes):
the exclusive bind refused a `SO_REUSEADDR` intruder on `0.0.0.0` and `127.0.0.1` (WSAEACCES
10013), while two `SO_REUSEADDR` sockets did share a port, the hazard the option closes. After
serving requests and stopping, the port held 4 server-side `TIME_WAIT` connections and the
exclusive rebind still succeeded at once, so `TIME_WAIT` does not block a restart. Run through a
venv's `python.exe`, the listener was the base interpreter, a child of the launcher; the owner
lookup named the child as a supervisor, killing it ended the launcher too, and the port rebound
at once. Without elevation (a `LIMITED` scheduled task) the lookup, the kill and the rebind all
worked the same. The five claim outcomes (live, stale after serving requests, silent, foreign,
stopping) behaved as above against real holders.

**Unsettled:**

- Not run on a unit, as `mast`. A predecessor run by another account (an operator's own
  Remote Desktop login) would refuse the kill, and the new supervisor aborts.
- A supervisor matched by `-m supervision` only. The service (stage 5) may start it another
  way, and then `runs_module` needs to learn that.
- A peer whose clock was stepped (NTP at boot) could look stale for one comparison. Both
  run on the same machine's wall clock, so this was judged unlikely to matter.

**Implications:** stage 5's service restarts a supervisor only after the old one exits, so it
never meets `stopping`. A fleet poller treats `ECONNREFUSED` as a supervisor not running,
since none runs without its port. The design's §7 sentence is replaced in mast-claude-config.
