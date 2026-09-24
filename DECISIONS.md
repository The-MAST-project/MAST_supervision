# Decisions

Newest first.

## [2026-09-24] Dependencies in pyproject.toml, lower bounds on shared libraries

**Why.** Every other MAST repo declares dependencies in `requirements.txt` (+ `requirements-dev.txt`). A new repo is the cheap moment to use the standard manifest instead, and uv — which the fleet already installs with — reads `pyproject.toml` directly, dependency groups included.

**What.** `pyproject.toml` with `[project].dependencies`, a `dev` dependency group (pytest, `ruff==0.16.0`), and `[tool.uv] package = false`: the repo is imported through `mast.pth`, never built or installed, so there is no `[build-system]`. Because `mast-clone` resolves all cloned repos into one venv in a single `uv pip install`, a library shared with another MAST repo takes a lower bound only (that repo's exact pin decides), and a library only this repo uses is pinned exactly. `uv.lock` is committed for this repo's CI and dev venvs; the fleet does not read it. Ruff configuration stays in a separate `ruff.toml`, matching the other repos so the files diff cleanly against each other.

**Implications.** `mast-clone` must learn to pass `-r <dir>/pyproject.toml --group <dir>/pyproject.toml:dev` when a repo has no `requirements.txt` — verified to resolve jointly with MAST_unit's `requirements.txt` under uv 0.11. Until that lands, provisioning cannot install this repo's dependencies.

## [2026-09-24] A separate repo, not a package in MAST_common

**Why.** The supervisor design first placed the code at `common/supervisor/`, to reuse common's existing clone, `mast.pth` entry and CI. But MAST_common is a library installed on the Linux control host and in CI images, and the supervisor is an application: a Windows service, win32 and tkinter bindings, a long-running process. Inside common it needed fences (win32 in two files only, the GUI dependency kept out of common's requirements, a refusal on the control host), and it would have shipped with every common pull made for the app's sake — including onto a feature branch someone checked out in the shared clone. The cost of a separate repo turned out small: provisioning's clone manifest takes one row, and the existing `mast.pth` already puts `<top>` on `sys.path`.

**What.** `mast-service` and `mast-supervisor` live together in MAST_supervision, cloned as `<top>/supervision/`, with the repo root being the `supervision` package. What another program would import stays in MAST_common: the `SupervisorConfig` schema (it sits beside `UnitConfig`/`SpecsConfig`), the port constants, `opmode`, and the process-lookup helpers. Agreed with Arie, 2026-09-24.

**Implications.** The repo is pinned independently in `mast-repos.tsv`, so the supervisor can stay on a known-good revision while common moves. The name collides with an unrelated PyPI package, `supervision`; `tests/test_package_location.py` fails if the import ever resolves anywhere but this checkout.
