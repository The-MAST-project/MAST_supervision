---
decided: 2026-11-12
status: proposed
issue: MAST_provisioning#82
areas:
  - services
  - process-supervision
---

# The session bridge goes through pywin32, starts the supervisor windowless inside a job, and lets interactive users only wait on the stop event

**Why:** `mast-service` runs as LocalSystem in session 0 and must start the supervisor in the autologon `mast` session, hold it so that a dead service cannot orphan it, and ask it to stop. Design §4 (mast-claude-config `plans/supervisor-design.md`) gives the calls. The job-object test of 2026-11-04 on labcomp2 (Windows 11, from a SYSTEM scheduled task) showed the backstop works across sessions: the child ran in session 1 inside the job, closing the job killed it within 3 s, and a `CREATE_BREAKAWAY_FROM_JOB` grandchild survived. That test was written in plain `ctypes`.

**What:**
- `session.py` holds `SessionBridge`, the `Win32SessionApi` protocol it is tested against, and `PyWin32SessionApi`, its pywin32 implementation. Nothing calls it yet; `service.py` will.
- `SessionBridge.spawn(application, command_line, cwd)` takes the active console session's user token (`WTSQueryUserToken`), makes it primary (`DuplicateTokenEx`), builds the user's environment (`CreateEnvironmentBlock`), creates the process with `CREATE_SUSPENDED | CREATE_UNICODE_ENVIRONMENT | CREATE_NO_WINDOW` on `winsta0\default`, assigns it to the job, and only then resumes it. A process the job refuses is terminated, not left suspended, and the error is raised.
- No console session (`0xFFFFFFFF`, or session 0) or nobody logged on (`ERROR_NO_TOKEN`) spawns nothing and returns `Waiting` with a retry after a doubling wait from 1 s to 60 s, as design §4 sets; the state change is logged once.
- `create_job()` makes the job with `KILL_ON_JOB_CLOSE | BREAKAWAY_OK`. `create_stop_event()` makes `Global\mast-supervisor-stop`, manual-reset, with the DACL `D:P(A;;GA;;;SY)(A;;GA;;;BA)(A;;0x00100000;;;IU)`.
- **pywin32, never `ctypes`.** `tests/conftest.py` blocks `win32process.CreateProcess` and `CreateProcessAsUser`; a `ctypes` call would go around it. `tests/test_import_boundaries.py` fails on any `ctypes` import in the package.
- **`CREATE_NO_WINDOW`, not design §4's `CREATE_NEW_CONSOLE`.** A console has a close button that kills the supervisor, and closing the supervisor's window must only minimize it (2026-10-11 window record).
- **Interactive users get `SYNCHRONIZE` on the stop event, not the `mast` account.** The supervisor runs as whoever autologon signs in; naming `IU` needs no account lookup in session 0 and matches the restart task's DACL (`(A;;0x1200a9;;;IU)`, 2026-10-18 record). `SYNCHRONIZE` alone lets it wait but not signal, so only SYSTEM and Administrators can ask for a stop.
- **`application` is passed explicitly**, the full path of the executable, so Windows never derives the program from the command line's first word. The pywin32 stubs also require it.
- `Backoff` moves from `managed.py` to `backoff.py`, which imports nothing. `service.py` will need it, and `managed.py` imports `common.mast_logging`, which builds a `Filer`: session 0 must not. `session.py` logs through the standard `logging` module for the same reason.

**Rejected:**
- Raw `ctypes`, as in the 2026-11-04 test: it bypasses the spawn guard, and pywin32 is already a dependency.
- A DACL naming the `mast` account: it would need the account resolved in session 0, and break on a machine whose autologon user is named otherwise.

**Unsettled:**
- `PyWin32SessionApi`'s spawn path has not run on Windows: the CI Windows leg tests only the job's limits and the event's signal and wait, which spawn nothing. The first real spawn is the dev-VM acceptance.
- The 2026-11-04 test ran on Windows 11 from the Task Scheduler. The fleet runs Windows 10 19044 under NSSM, which puts its service in no job; whether that changes anything is for acceptance items 3 and 8 of the service plan.
- Whether `CREATE_NO_WINDOW` leaves the supervisor's own window (tkinter) visible on the interactive desktop is believed but not checked; it is for acceptance item 13.

**Implications:** `service.py` (piece S4) creates the job and the event once, calls `spawn` until it returns `Spawned`, waits on the process handle, and closes the job on a hard stop. The supervisor side (S3) opens the event by name and waits on it.
