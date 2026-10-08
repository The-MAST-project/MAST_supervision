---
decided: 2026-10-08
status: accepted
issue: MAST_supervision#9
areas:
  - dependency management
  - reproducibility
---

# The dev venv is built the way CI builds it, by one script, and `uv sync` is not used

**Why:** The documented setup was `uv sync`, then `PYTHONPATH=.. uv run pytest`. `uv sync`
builds `.venv` from this repo's `pyproject.toml` and `uv.lock` only, and those deliberately
do not declare MAST_common's dependencies (2026-09-24 dependency record), so collection
failed on `rich` and `pymongo` (MAST_supervision#9). CI never hit it: its install step
resolved `common/requirements-ci.txt` and this manifest together. Checked on 2026-10-08 with
uv 0.11.33: a venv built that way passes the suite. `uv run` leaves the extra packages in
place, but `uv sync` removes them again.

**What:** `tools/dev-env.sh` holds the one install: `uv pip install -r
common/requirements-ci.txt -r <repo>/pyproject.toml --group <repo>/pyproject.toml:dev`, run
from `<top>` with relative paths. Without arguments it installs into `<repo>/.venv`, creating
it with Python 3.12 if it is missing; that also repairs a venv a sync emptied. With
`--system`, CI's test job installs into the runner's interpreter. README and CLAUDE.md run the
checks as `.venv/bin/python -m …` and say not to run `uv sync`. `uv.lock` stays, pinning CI's
lint job (`uv run --only-group dev`). Decided by Eli on 2026-10-08.

**Rejected:**

- *Declaring MAST_common's dependencies in `pyproject.toml`, so `uv sync` works* — a second
  copy of MAST_common's manifest, which the 2026-09-24 record rules out.
- *Documenting the install line in the README and leaving CI's inline copy* — two copies of the
  line, free to drift.

**Unsettled:** The script uses the `uv` on `PATH`. CI pins 0.11.33, but a developer's uv may
be older; nothing checks the version, as `mast-clone` does on the fleet.

**Implications:** CI's install and the developer's install change together. A dependency
MAST_common adds reaches both through `common/requirements-ci.txt`.
