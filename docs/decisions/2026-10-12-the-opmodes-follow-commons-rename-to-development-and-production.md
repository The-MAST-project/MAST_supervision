---
decided: 2026-10-12
status: accepted
areas:
  - source-layout
  - process-supervision
---

# The opmodes follow common's rename to `development` / `production`

**Why:** MAST_common#153 renamed the opmodes `operated` / `controlled` to `development` /
`production`. Common still reads the old names, but `resolve_opmode()` returns the new member,
and `state.OpMode` is a copy with its own values, so `wiring.py`'s `OpMode(resolve_opmode())`
raised `ValueError` on every machine that had pulled common. `test_local_enum_copies_match_common`
flagged it.

**What:** `state.OpMode` holds only `DEVELOPMENT = "development"` and
`PRODUCTION = "production"`, matching the members common iterates. The launcher's `match`, its
refusal message and the docstrings use the new names, and `CONTROLLED_ROLES` is now
`PRODUCTION_ROLES`. The 2026-10-11 decision on the launcher stands; read its `operated` /
`controlled` as `development` / `production`.

**Rejected:**

- *Copying common's deprecated aliases into `state.OpMode`* — this repo never parses a stored
  value itself. Every value comes through `resolve_opmode()`, which maps the old names already.

**Implications:** The supervisor reports and logs `opmode=development` / `production` from now
on. Open PRs that assert the old strings (MAST_supervision#16) need the same rename.
