---
decided: 2026-11-12
status: proposed
issue: MAST_provisioning#82
areas:
  - services
  - process-supervision
---

# mast-service is registered by an idempotent installer whose stop waits out the teardown and spares the programs

**Why:** stage 5 runs the supervisor under `mast-service`, an NSSM service, and
MAST_provisioning's `mast-service` provider (MAST_provisioning#262) runs this repo's installer
on a supervised unit and verifies the registration: nssm-hosted, `LocalSystem`, automatic start,
`Application` `<Top>\.venv\Scripts\python.exe`, `AppParameters` `-m supervision.service`,
`AppDirectory` `<Top>`. The installer is the only place `nssm install mast-service` happens:
provisioning's no-service scan stops at its own tree. Two stop facts shaped it, both from
reading the code on 2026-11-12: NSSM 2.24's defaults give each stop method about 1.5 s and then
kill the process tree, and the supervisor's teardown (S1) can take minutes.

This is piece S5 of the vault's *MAST supervisor service plan* (K5, K6, R2, R10 there).

**What:** `install/install-mast-service.ps1`, run elevated from `<Top>\supervision\install\`:

- **Idempotent.** `nssm install` only when the service is absent; then every parameter is set
  on every run (`nssm set`), so a re-run converges a changed registration. A service of that
  name that nssm does not host is refused. It never starts the service: provisioning's
  finalize does, and its standdown stops it around every run.
- **Identity:** `LocalSystem`, set explicitly as `ObjectName` rather than left to `nssm
  install`'s default, so a re-run also undoes a hand change; `SERVICE_AUTO_START`.
- **Logs:** NSSM's capture of the application's stdout and stderr goes to
  `C:\MAST\logs\mast-service\nssm-stdout.log` / `nssm-stderr.log`, rotated online at 10 MiB.
  `service.py` keeps its own log beside them.
- **Restart:** `AppRestartDelay 5000`; and an `sc.exe failure` triple (restart after 5 s,
  three times, reset after a day) for `nssm.exe` itself dying, which NSSM cannot handle.
- **Stop:** NSSM sends Ctrl+C, waits `AppStopMethodConsole`, then terminates `service.py`.
  `AppStopMethodSkip 6` skips `WM_CLOSE` and `WM_QUIT`, since `service.py` has no window and no
  message loop. `AppStopMethodConsole` is the supervisor's whole teardown, a probe in flight
  (70 s, `APP_STATUS_TIMEOUT_SECONDS`), the app's shutdown wait (200 s,
  `APP_SHUTDOWN_WAIT_SECONDS`), two stop graces for each of the four torn-down programs
  (2 x 10 s x 4, `RestartPolicy.stop_grace_seconds`, `TEARDOWN_ORDER`), plus 60 s: 410 s. A test
  keeps the components equal to the Python constants. `AppKillProcessTree 0`: NSSM kills only
  `service.py`. The supervisor then dies with the job `service.py` holds (kill on close), and
  the supervised programs, spawned with `CREATE_BREAKAWAY_FROM_JOB`, survive, which is design §4's
  backstop. NSSM's default tree kill would have killed them as the supervisor's descendants.
- **`-Uninstall`** stops the service and runs `nssm remove mast-service confirm`, for use by
  hand. Provisioning removes the service with its own `Remove-MastService` (K5), which works on a
  checkout that predates this file.
- `tests/test_install_mast_service.py` pins each value against the provider's verify, and CI
  gains a `pssa` job: PSScriptAnalyzer 1.25.0 over `install/`, with `PSScriptAnalyzerSettings.psd1`.

**Rejected:**

- *NSSM's default stop (1.5 s per method, tree kill).* It would cut the teardown short and kill
  the programs out of order: exactly what the job backstop exists to avoid.
- *Omitting `ObjectName`* (the plan's sketch): `LocalSystem` is `nssm install`'s default, but
  only on install. Set every run, a re-run converges it like every other parameter.
- *`-Uninstall` as provisioning's removal path:* removal must work on a unit whose pinned
  checkout has no installer.

**Unsettled:**

- `service.py` (S4) was being written in parallel and decides how it reacts to Ctrl+C, and its
  own grace between signaling `Global\mast-supervisor-stop` and closing the job. That grace must
  fit inside `AppStopMethodConsole`. If S4 needs a different budget, the constants move here.
- The provisioning run stops the service with `Stop-Service` in standdown; whether the SCM
  waits the full 410 s for a service in `STOP_PENDING` was not checked on the fleet. NSSM keeps
  reporting progress while it waits, which is understood to keep the SCM waiting.
- At Windows shutdown the SCM gives services only `WaitToKillServiceTimeout`, far below 410 s,
  so the teardown is cut short there (the plan's R5).
- PSScriptAnalyzer and the script itself never ran on a Mac (no `pwsh`); they run first in CI
  and on the labcomp2 dev VM.

**Implications:** the dev-VM acceptance (the plan's items 2, 3, 8 and 9) checks the stop order,
its duration and that the programs survive a kill of the service. `C:\MAST\logs\mast-service\` is
created by the installer, before `service.py` first runs.
