# MAST_supervision

Process supervision for MAST unit and spec machines: two programs, one package.

| program | runs as | job |
|---|---|---|
| `mast-service` | Windows service (NSSM, LocalSystem, session 0) | starts and watches `mast-supervisor` in the interactive session |
| `mast-supervisor` | the autologon `mast` session | waits for network / RAM disk / share, reads the config DB, owns PWI4 / PHD2 / ps3cli, launches VSCode or the unit/spec app according to `opmode`, and reports its state |

The supervisor reports through three surfaces built on one snapshot: a small window with a rolling log, a heartbeat block in the log file every five minutes, and `GET /mast/api/v1/supervisor/status` on port 8004.

On a unit, it also revives the RAM disk (`D:`) when it is gone, on its own probe or when asked through `PUT /mast/api/v1/supervisor/ramdisk/restart` on `127.0.0.1:8005` (`Const.SUPERVISOR_ACTIONS_PORT`). That port is not reachable from another machine, since restarting wipes `D:`. The answers: `409` means `D:` is writable and nothing was done, `202` means a revival is running (ask again), `200` means it was restored (also for 5 minutes afterwards, so asking again hears the outcome), `502` means this revival failed (the supervisor keeps reviving on its own probe), and `503` means it gave up after 3 failed or short-lived revivals within an hour. The revival runs the SYSTEM scheduled task `\mast-ramdisk-restart`, which MAST_provisioning's imdisk provider registers. The supervisor is not elevated, and cannot detach a disk itself.

A supervised program can be held down through `PUT /mast/api/v1/supervisor/programs/{name}/stop` on the same loopback listener, and released through `.../programs/{name}/start`. A held program is stopped (terminate, then kill after the grace) and the supervisor does not restart it; start lets it be found and started again. This is the way to keep PWI4, ps3cli or a `production` app down without stopping the whole supervisor, which would otherwise restart them. The answers: `202` means the request is queued for the loop's next tick (read `/status` for the outcome, where the row reads `stopped` and the name is listed under `held`), `404` means there is no program of that name, or the configuration has not loaded yet, and `409` means the program is observed or not supervised, so not the supervisor's to stop: PHD2, which the app starts, and a `development` app, which the operator starts. **A hold is not remembered:** a supervisor that restarts starts every program again.

**Status: stage 3 in progress.** The supervisor runs from a console (`python -m supervision --no-service`) with its read-only window, or headless with `--no-gui`, and serves `GET /status`; the window's Actions menu and the service are still to come. Landed so far:

| module | what |
|---|---|
| `state.py` | `SupervisorSnapshot`, the one model the window, heartbeat and status API render; its `severity` is the worst resource or process state, and `not_supervised` (VSCode under `development`, reported with no mode) is never a fault. Imports nothing from `common.config` |
| `logsink.py` | `DequeHandler`, the bounded cross-thread buffer between the root logger and the window |
| `gui_model.py` | what the window shows, with no tkinter so it is tested on Linux: `drain()`, the per-tick log drain; `header()`, the table rows and the About text, from a snapshot |
| `gui.py` | the read-only window (ttkbootstrap's `bootstrap-dark` theme, plain Tk without it): header, resource and process tables, the log pane with a level filter and tail-follow, About and Help. Closing minimizes it; a machine with no window runs headless |
| `resources.py` | the network (config DB reachable), RAM-disk-and-indexes (against provisioning's `C:\MAST\state\mast-indexes.json`, by name and size; not checked where there is none; unit only) and share probes, and `ResourceTracker`, which waits each out for its budget and then lets the supervisor proceed degraded |
| `clock.py` | the injected monotonic and wall clocks |
| `probes.py` | the PWI4, PHD2, ps3cli and app health probes, each one bounded call against a local port with a timeout no stricter than the app's own client, imports no client of the programs it probes |
| `managed.py` | `ManagedProcess`: adopts a copy in its own login session or spawns one, blocks without killing when a copy runs in another session, probes without overlap, restarts after the threshold with backoff and a startup grace, stops on a crash loop; `OsProcessApi` is the Windows side |
| `launcher.py` | `build_launch()`, the pure role + `opmode` -> `LaunchSpec` function: VSCode once under `development`; the app under `production`, for unit only (spec's app does not read `opmode` yet), without proxy variables, with `MAST_OPMODE` only as inherited, and with `MAST_LAUNCH_OPMODE` set to the launch opmode, which the app reads only where its configuration cannot say |
| `locate.py` | where VSCode, PWI4 and ps3cli (and its catalog) are installed, when the configuration names no `exe` |
| `programs.py` | what each configured program name means on a MAST machine (image, location, default arguments, probe), resolved into the rows the loop supervises and the lines `--dry-run` prints |
| `heartbeat.py` | `render_heartbeat()`: the whole snapshot as one ASCII log record, at its severity |
| `supervisor.py` | the loop: waits out the resources, loads the configuration on a backoff, supervises the programs, launches VSCode or the app, republishes the snapshot on change and on every heartbeat; stopping publishes `stopping` and, by default, leaves the programs running; `run(stop, StopFlavor.TEAR_DOWN)` instead stops the app, PHD2, PWI4 and ps3cli in that order, sending a started-up unit app its `shutdown` first (the service's stop, not yet wired); `request_program()` queues a hold or release from the loopback API, applied at the next tick |
| `ramdisk.py` | `RamdiskReviver`: probes `D:` with a write before acting, runs one revival at a time through the `\mast-ramdisk-restart` task, waits until `D:` is writable again with its index folder (missing indexes are reported, not retried), and gives up after 3 failed or short-lived revivals within an hour. Unit only |
| `api.py` | `GET /mast/api/v1/supervisor/status`: the latest snapshot in a `CanonicalResponse`, read-only, on uvicorn in a daemon thread, served from `starting` on; and `build_action_app()`, `/status` plus the `PUT /ramdisk/restart` and `PUT /programs/{name}/stop|start` routes, served only on the loopback listener; `start_servers()` starts both |
| `instance.py` | `claim_listeners()`: claims the status listener (`0.0.0.0:8004`) and the action listener (`127.0.0.1:8005`) alike, through `claim_port()`, and the supervisor does not start without both. The status port is the one-supervisor-per-machine lock. A live supervisor on it aborts this one; a `stopping` one is waited for; a dead or wedged supervisor is killed and its programs adopted; a holder that is not a supervisor aborts this one |
| `service.py` | `mast-service`, the NSSM application (`python -m supervision.service` from `<top>`): holds the `Global\mast-service` mutex (a second copy exits with code 2), starts `python -m supervision --service` in the console session inside the job, polls its handle every second and restarts it after a wait doubling from 1 s to 60 s, and on NSSM's console Ctrl-C signals `Global\mast-supervisor-stop`, waits up to 380 s for the teardown, then closes the job. Logs to `C:\MAST\logs\mast-service\`, imports nothing that builds a `Filer`. The installer and the supervisor's `--service` flag are separate pieces |
| `session.py` | the session bridge `service.py` uses: `SessionBridge.spawn()` starts a program in the console session as its user, suspended, windowless, on `winsta0\default`, assigns it to the job and then resumes it, and waits (1 s doubling to 60 s) when nobody is logged on; `create_job()` (kill on close, breakaway allowed) and `create_stop_event()` (`Global\mast-supervisor-stop`, interactive users may only wait on it). pywin32 only, imported inside `PyWin32SessionApi`, which also has the mutex and the waits `service.py` uses |
| `backoff.py` | `Backoff`, doubling waits up to a cap; imports nothing, so session 0 can use it |
| `wiring.py` | the real configuration, probes and process API the loop is given; the tests give it fakes |
| `__main__.py` | `--dry-run` prints the role, `opmode`, the programs and the launch; `--no-service [--no-gui] [--no-app]` claims ports 8004 and 8005, serves `/status` on both and the action routes on `127.0.0.1:8005` (a unit only), opens the window with the loop on a worker thread, and runs until Ctrl-C, logging to `mast-<role>-supervisor-log.txt`; it exits 1 when either port cannot be had |

The design is [`plans/supervisor-design.md`](https://github.com/The-MAST-project/mast-claude-config/blob/main/plans/supervisor-design.md) in mast-claude-config.

## Layout on a machine

MAST_supervision is cloned by MAST_provisioning's `mast-clone` as a sibling of the other MAST repos, and imported through the `mast.pth` in the role's shared venv — it is not pip-installed:

```
<top>/
  .venv/           one venv for the role; mast.pth puts <top> on sys.path
  common/          MAST_common
  unit/ or spec/   the app
  supervision/     this repo; its root IS the `supervision` package
```

The folder must be named `supervision`, as `common` must be named `common`.

## Dependencies

Declared in `pyproject.toml` (not `requirements.txt`). On the fleet, `mast-clone` resolves this file together with the other cloned repos' manifests in one `uv pip install`, so:

- libraries another MAST repo also uses (fastapi, uvicorn, pydantic, pywin32) carry **lower bounds only**, and that repo's exact pin decides the version;
- libraries only this repo uses are **pinned exactly** here;
- `uv.lock` pins the environment of CI's lint job (`uv run --only-group dev`). The tests run in the joint install `tools/dev-env.sh` makes, and **the fleet does not read the lock**: a unit runs whatever the joint resolve produced.

`supervision` imports `common` but does not declare MAST_common's dependencies; on the fleet they come from the app's manifest, and in CI and development from `common/requirements-ci.txt`, through `tools/dev-env.sh`.

## Development

From a `<top>` holding `common/` and `supervision/`:

```sh
cd supervision
tools/dev-env.sh             # .venv: MAST_common's CI pins + this manifest, one resolve, as CI installs
PYTHONPATH=.. .venv/bin/python -m pytest
.venv/bin/python -m ruff format --check . && .venv/bin/python -m ruff check .
.venv/bin/basedpyright         # types, per pyrightconfig.json; blocking in CI
```

**Do not run `uv sync` here.** It rebuilds `.venv` to match `uv.lock` exactly, which removes MAST_common's dependencies, and the suite then cannot import `common`. Rerun `tools/dev-env.sh` to repair a venv it emptied.

`tests/conftest.py` makes every process launch raise (`subprocess`, `os`, `psutil.Popen`, and `win32process.CreateProcess` / `CreateProcessAsUser`), since the suite runs on machines where these programs drive hardware. It also redirects `common`'s `Filer` to a temp dir, which is what lets the tests import `common.config` on a Mac.

CI (`.github/workflows/ci.yml`) runs the tests on Linux and Windows against the MAST_common branch named like the PR's head branch, else like its base branch, else `master`, type-checks with basedpyright on the Linux leg (`pythonPlatform: All` covers the win32 branches), and lints on Linux.

**Supervisor work lands on `main`.** A change that needs an unmerged MAST_common change goes on a branch named like common's, so CI pairs the two.

## Decision records

Design rationale lives in `docs/decisions/`, one dated file per decision, in the format MAST_provisioning defined (summarized in `docs/decisions/README.md`). `ls docs/decisions/2*.md` is the index.

## Related repos

- [MAST_common](https://github.com/The-MAST-project/MAST_common) — the config schema (`config/supervisor.py`), the port constants, `opmode`, and the process helpers this repo builds on.
- [MAST_provisioning](https://github.com/The-MAST-project/MAST_provisioning) — clones this repo, installs NSSM and autologon. Epic: [MAST_provisioning#82](https://github.com/The-MAST-project/MAST_provisioning/issues/82).
