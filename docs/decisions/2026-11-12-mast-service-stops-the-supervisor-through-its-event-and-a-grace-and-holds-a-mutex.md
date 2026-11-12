---
decided: 2026-11-12
status: proposed
issue: MAST_provisioning#82
areas:
  - services
  - process-supervision
---

# mast-service stops the supervisor through its stop event and a derived grace, is stopped by NSSM's console Ctrl-C, and holds a mutex

**Why:** `service.py` is what NSSM runs as `mast-service`, in session 0 as LocalSystem. It has to keep one supervisor alive in the console session, and when the service stops it has to give that supervisor time to tear the programs down in order (the 2026-11-12 teardown record) before the job kills it. NSSM decides how a stop reaches its application, and the supervisor's teardown has a known worst case, so both have to be settled here and matched by the installer.

**What:**
- **One copy, by mutex.** `service.py` first takes `Global\mast-service`, owned. If it already exists, it logs and exits with code 2, starting nothing. Killing `nssm.exe` alone leaves `service.py` running; when the service control manager starts NSSM again, its new `service.py` must not start a second supervisor beside the orphan's. The supervisor's own port lock (the 2026-10-11 status-port record) would refuse the second one anyway; the mutex keeps it from being spawned at all.
- **Watching.** The supervisor is started as `"<python>" -m supervision --service` from `<top>`, through the session bridge into the kill-on-close job (the 2026-11-12 session-bridge record), with `<python>` being `sys.executable`, the venv's interpreter NSSM started. Its process handle is waited on in 1 s polls. When it exits it is restarted after a wait doubling from 1 s to 60 s; a supervisor that ran 60 s or more starts the doubling again from 1 s. No console session waits on the bridge's own backoff and never fails. Every wait is slept in slices of at most one poll, so a stop is seen within a second.
- **Stop.** On stop, `service.py` signals `Global\mast-supervisor-stop`, waits up to `SUPERVISOR_STOP_GRACE_SECONDS` for the supervisor to exit, then closes the job, which kills a supervisor still there. The job is closed whether or not the wait succeeded, and whether or not a supervisor was running.
- **The grace is derived, not guessed:** the app's own shutdown wait (`APP_SHUTDOWN_WAIT_SECONDS`, 200 s) + terminate-then-kill for each program in the teardown (4 × 2 × `stop_grace_seconds`' default of 10 s) + a probe the loop waits for at exit (`APP_STATUS_TIMEOUT_SECONDS`, 70 s, the non-daemon executors) + a 30 s margin = **380 s**. Each input is a copy, because `probes.py` and `supervisor.py` import `common.mast_logging`, which builds a `Filer`; each copy is pinned to its source by a test. A unit configured with a larger `stop_grace_seconds` than the default outgrows it; the job then kills the supervisor partway through the teardown.
- **NSSM stops it with a console Ctrl-C.** NSSM 2.24 tries, in order, a Ctrl-C on the application's console (`AppStopMethodConsole`), `WM_CLOSE` to its windows (`AppStopMethodWindow`), `WM_QUIT` to its threads (`AppStopMethodThreads`) and `TerminateProcess`, each with a 1.5 s default wait. A windowless Python has no window and no message loop, so only the Ctrl-C reaches it. `service.py` turns `SIGINT` (and `SIGBREAK`) into setting the stop event rather than raising `KeyboardInterrupt`, which could land inside a Win32 call and skip the teardown. Python runs a signal handler between bytecodes, which is why the watch polls in 1 s slices rather than blocking on the handle.
- **What the installer (S5) must set** so that this works:
  - `AppStopMethodConsole` above the grace: **420000** ms (380 s + 40 s for closing the job and logging);
  - `AppStopMethodSkip` **6**: skip the window and thread methods, which cannot reach it and would only add their waits; keep the console method and the final terminate;
  - `AppKillProcessTree` **0**: NSSM's tree kill follows parent PIDs, and the programs the supervisor starts with `CREATE_BREAKAWAY_FROM_JOB` are still its children by PID, so a tree kill would take PWI4, PHD2 and the app with it. Design §4 wants a hard stop to remove only the supervisor, which the job already does;
  - `AppNoConsole` **0** (the default): the Ctrl-C needs a console to arrive on.
- **Logging** is the standard library's, to `C:\MAST\logs\mast-service\mast-service.log`, rotated at 10 MiB with five backups. `service.py` imports nothing that builds a `Filer`, reads the configuration or calls MAST_common's `configure_logging()`; `tests/test_import_boundaries.py` checks its module-level imports.
- `PyWin32SessionApi` gains `create_mutex` and `close`, so all Win32 stays in `session.py`.

**Implications:**
- The service's stop can take up to about seven minutes, almost all of it the app's shutdown. NSSM keeps reporting stop-pending to the service control manager meanwhile, so `sc stop` waits it out; a Windows shutdown does not, but the user session, and the supervisor in it, is logged off before services stop (R5 in the deployment plan).
- With `AppKillProcessTree 0`, a terminate that reaches the venv's `python.exe` redirector leaves the real interpreter, and so `service.py`, running. NSSM's terminate only follows a console stop that ran out its 420 s, or a `taskkill /f`. The orphan keeps its supervisor, and each `service.py` NSSM starts after it exits on the mutex until the orphan ends. Acceptance item 8 checks this on the dev VM.
- `--service` is the supervisor's flag from S3 (MAST_supervision, `eli/service-launch`); the two must merge before anything runs this.

**Rejected:**
- *A mutex wait instead of an exit:* a second `service.py` waiting for the orphan's mutex would take over when the orphan dies, but it cannot stop the orphan's supervisor without also signaling the shared stop event, which the orphan would answer by restarting it. Exiting keeps one owner.
- *Reading `stop_grace_seconds` from the configuration:* session 0 does not read the configuration (design §4).
- *A blocking wait on the process handle:* a Ctrl-C would not be handled until the supervisor exited.

**Unsettled:**
- Whether Ctrl-C reaches `service.py` through the venv redirector on the fleet's Windows 10 19044 (the redirector ignores Ctrl-C and waits for its child, which shares its console). Acceptance item 2.
- The real `create_mutex` and `close` run only on Windows, and nothing in CI calls them; the dev-VM acceptance is their first run.
