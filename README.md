# MAST_supervision

Process supervision for MAST unit and spec machines: two programs, one package.

| program | runs as | job |
|---|---|---|
| `mast-service` | Windows service (NSSM, LocalSystem, session 0) | starts and watches `mast-supervisor` in the interactive session |
| `mast-supervisor` | the autologon `mast` session | waits for network / RAM disk / share, reads the config DB, owns PWI4 / PHD2 / ps3cli, launches VSCode or the unit/spec app according to `opmode`, and reports its state |

The supervisor reports through three surfaces built on one snapshot: a small window with a rolling log, a heartbeat block in the log file every five minutes, and `GET /mast/api/v1/supervisor/status` on port 8004.

**Status: skeleton.** Nothing is implemented yet. The design is [`plans/supervisor-design.md`](https://github.com/The-MAST-project/mast-claude-config/blob/main/plans/supervisor-design.md) in mast-claude-config.

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

CI (`.github/workflows/ci.yml`) runs the tests on Linux and Windows against the MAST_common branch of the same name when one exists, else `master`, and lints on Linux.

## Related repos

- [MAST_common](https://github.com/The-MAST-project/MAST_common) — the config schema (`config/supervisor.py`), the port constants, `opmode`, and the process helpers this repo builds on.
- [MAST_provisioning](https://github.com/The-MAST-project/MAST_provisioning) — clones this repo, installs NSSM and autologon. Epic: [MAST_provisioning#82](https://github.com/The-MAST-project/MAST_provisioning/issues/82).
