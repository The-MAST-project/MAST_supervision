---
decided: 2026-09-27
status: accepted
areas:
  - source-layout
  - status-reporting
---

# `state.py` copies `SupervisionMode` and `OpMode` rather than importing `common.config`

**Why:** `state.py` first imported `SupervisionMode` from `common.config.supervisor`, and
`gui_model.py` imported `GuiConfig` from the same package. Importing anything under
`common.config` runs `common/config/__init__.py`, which imports pymongo and `common.utils`,
and `common/utils.py` builds a module-level `Filer()` — so `import supervision.state` reached
`Filer()` at import time, against this repo's CLAUDE.md rule, and raised outright on a Mac
without the test shim. The snapshot is also the model a second program (a MAST_gui fleet
view, per the design's §2) is expected to import, which would inherit the same cost. Found in
review; the root cause is in MAST_common, and fixing it there would have widened
MAST_common#130 while it waited for review.

**What:** `state.py` defines its own `SupervisionMode` and `OpMode`, value for value copies of
`common.config.supervisor.SupervisionMode` and `common.opmode.OpMode`.
`tests/test_supervisor_state.py::test_local_enum_copies_match_common` pins each copy's
members to the original's, so a drift fails CI. `gui_model.drain()` takes its three limits
as plain arguments instead of a `GuiConfig`; `gui.py` will read them from the config and pass
them. Neither module now imports `common.config`. Decided with Eli on 2026-09-27, under the
copy-don't-consolidate rule of 2026-09-24.

**Rejected:**

- *Importing from `common.config` and accepting the cost* — breaks the import-time rule for
  the one module meant to be shared.
- *Fixing `common.config`'s import chain in MAST_common#130* — right in the end, wrong to
  bundle into a PR under review.

**Unsettled:** `common.opmode` alone is light (it imports `common.mast_logging`, which does
not build a `Filer` at import), so importing `OpMode` from it would have worked; it was copied
anyway so that both enums have one treatment and one consolidation item.

**Implications:** Row 10 of MAST_supervision#2 — consolidating means a MAST_common module
importable without `common.config`'s side effects, after which both copies are deleted.
