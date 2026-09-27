# MAST_supervision

Process supervision for MAST unit and spec machines: two programs, one package.

| program | runs as | job |
|---|---|---|
| `mast-service` | Windows service (NSSM, LocalSystem, session 0) | starts and watches `mast-supervisor` in the interactive session |
| `mast-supervisor` | the autologon `mast` session | waits for network / RAM disk / share, reads the config DB, owns PWI4 / PHD2 / ps3cli, launches VSCode or the unit/spec app according to `opmode`, and reports its state |

The supervisor reports through three surfaces built on one snapshot: a small window with a rolling log, a heartbeat block in the log file every five minutes, and `GET /mast/api/v1/supervisor/status` on port 8004.

**Status: stage 2 in progress** — the platform-neutral core, nothing yet runnable. Landed so far:

| module | what |
|---|---|
| `state.py` | `SupervisorSnapshot`, the one model the window, heartbeat and status API render; its `severity` is the worst resource or process state, and `not_supervised` (VSCode under `automatic`, reported with no mode) is never a fault. Imports nothing from `common.config` |
| `logsink.py` | `DequeHandler`, the bounded cross-thread buffer between the root logger and the window |
| `gui_model.py` | `drain()`, the window's per-tick log drain, with no tkinter so it is tested on Linux |
| `resources.py` | the network (config DB reachable), RAM-disk-and-indexes and share probes, and `ResourceTracker`, which waits each out for its budget and then lets the supervisor proceed degraded |
| `clock.py` | the injected monotonic and wall clocks |

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
- `uv.lock` pins this repo's own CI and dev environments. **The fleet does not read it**: a unit runs whatever the joint resolve produced.

`supervision` imports `common` but does not declare MAST_common's dependencies; on the fleet they come from the app's manifest, and in CI from `common/requirements-ci.txt`.

## Development

From a `<top>` holding `common/` and `supervision/`:

```sh
cd supervision
uv sync                      # dev venv from uv.lock
PYTHONPATH=.. uv run pytest
uv run ruff format --check . && uv run ruff check .
```

`tests/conftest.py` makes every process launch raise (`subprocess`, `os`, `psutil.Popen`, and `win32process.CreateProcess` / `CreateProcessAsUser`), since the suite runs on machines where these programs drive hardware. It also redirects `common`'s `Filer` to a temp dir, which is what lets the tests import `common.config` on a Mac.

CI (`.github/workflows/ci.yml`) runs the tests on Linux and Windows against the MAST_common branch named like the PR's head branch, else like its base branch, else `master`, and lints on Linux.

**Supervisor work lands on `supervision-integration`, not `main`.** The supervisor needs MAST_common changes that are not yet on `master`, so both repos carry a `supervision-integration` branch: feature PRs target it here, CI pairs it with common's branch of the same name, and the two merge to `main` / `master` together.

## Decision records

Design rationale lives in `docs/decisions/`, one dated file per decision, in the format MAST_provisioning defined (summarized in `docs/decisions/README.md`). `ls docs/decisions/2*.md` is the index.

## Related repos

- [MAST_common](https://github.com/The-MAST-project/MAST_common) — the config schema (`config/supervisor.py`), the port constants, `opmode`, and the process helpers this repo builds on.
- [MAST_provisioning](https://github.com/The-MAST-project/MAST_provisioning) — clones this repo, installs NSSM and autologon. Epic: [MAST_provisioning#82](https://github.com/The-MAST-project/MAST_provisioning/issues/82).
