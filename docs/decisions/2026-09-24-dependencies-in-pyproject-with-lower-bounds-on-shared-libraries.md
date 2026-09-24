---
decided: 2026-09-24
status: accepted
areas:
  - dependency management
  - reproducibility
---

# Dependencies live in pyproject.toml, with lower bounds on libraries shared with other MAST repos

**Why:** Every other MAST repo declared its dependencies in `requirements.txt` plus
`requirements-dev.txt`. A new repo was the cheap moment to use the standard manifest instead,
and uv — which MAST_provisioning's `tools/mast-clone.ps1` already installs the fleet with —
reads `pyproject.toml` directly, dependency groups included. The constraint that shaped the
pins is how `mast-clone` installs: every cloned repo's manifest in **one** `uv pip install`
into the role's single `<top>/.venv`, so that repos sharing a machine must agree on shared
pins, and a disagreement fails the install loudly.

**What:** `pyproject.toml` carries `[project].dependencies`, a `dev` dependency group
(pytest, and `ruff==0.16.0`, the fleet pin), and `[tool.uv] package = false`. The repo is
imported through `mast.pth` and never built, so there is no `[build-system]`. A library
another MAST repo also uses — fastapi, uvicorn, pydantic, pywin32 — takes a **lower bound**
at the fleet's current pin, and that repo's exact pin decides the installed version. A library
only this repo uses is **pinned exactly** here. `uv.lock` is committed and pins this repo's
own CI and dev environments. Ruff settings stay in a separate `ruff.toml`, as in the other
repos, so the files diff cleanly against each other.

On 2026-09-24 a joint resolve of MAST_unit's `requirements.txt` with this `pyproject.toml`
and `--group pyproject.toml:dev` was run under uv 0.11 and resolved, landing fastapi,
pydantic and uvicorn at MAST_unit's pins; the seed PR's CI ran the same joint resolve over
`common/requirements-ci.txt` on Linux and Windows.

**Rejected:**

- *`requirements.txt`*, matching the other repos — declined in favor of the standard
  manifest (`[project]` and `[dependency-groups]`), which uv installs as readily; matching
  the other repos was judged worth less than starting the new one on the current standard.
- *Exact pins on shared libraries* — they would have to move in lockstep with MAST_unit's and
  MAST_spec's, or the joint install fails for the whole role.
- *Installing the package* (a `[build-system]` and `pip install -e`) — would give the code two
  import paths, the installed one and `mast.pth`'s.
- *`[tool.ruff]` in `pyproject.toml`* — MAST_unit moved its ruff settings out of
  `pyproject.toml` into `ruff.toml`; keeping the same file keeps the fleet's configs
  comparable.

**Unsettled:**

- `mast-clone` looks only for `requirements.txt` / `requirements-dev.txt`. Until it learns
  to pass `-r <dir>/pyproject.toml --group <dir>/pyproject.toml:dev` for a repo without
  them, provisioning cannot install this repo's dependencies.
- The fleet does not read `uv.lock`: a unit runs whatever the joint resolve produced, which
  can differ from what this repo's CI tested.
- `supervision` imports `common` but does not declare MAST_common's dependencies. On the
  fleet they arrive through the app's manifest; a machine running the supervisor without an
  app repo would lack them.

**Implications:** This is the first MAST repo on `pyproject.toml`; the lower-bound rule is
the part the other repos would need to adopt it too.
