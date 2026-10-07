---
decided: 2026-09-27
status: superseded
superseded_by: 2026-10-07-supervisor-work-lands-on-main
areas:
  - source-layout
  - reproducibility
---

# Supervisor work lands on a `supervision-integration` branch paired across MAST_common and here

**Why:** Stage 1 of the supervisor lives in MAST_common (MAST_common#130: `opmode`,
`SupervisorConfig`, `find_processes`), and every later stage here builds on it. Eli chose to
defer merging #130 to `master`. Merging stage 2 into this repo's `main` meanwhile would leave
`main` ahead of common's `master`: the runtime modules import nothing from `common.config`,
but the tests do (the enum-copy pin and the conftest's `Filer` shim), so a push build on
`main`, which pairs with `master`, would fail. CI also paired only by the head branch's name,
so each of stage 2's five PRs would have needed a same-named branch in common.

**What:** Both repos carry `supervision-integration`, cut from `master` and `main` on
2026-09-27. MAST_common#130 and MAST_supervision#4 merge into it, and so do later stages. The CI
step *Resolve the paired MAST_common branch* tries the head branch's name, then the base
branch's (`github.base_ref`), then `master`, so a PR into `supervision-integration` builds
against common's `supervision-integration` whatever its own head is called. Pushes to
`supervision-integration` run CI too. The two branches merge to `main` / `master` together.
Decided with Eli on 2026-09-27.

**Rejected:**

- *Merging stage 2 into `main` now* — the push-build failure above.
- *One common branch per supervision PR* — a namesake per part is bookkeeping with nothing in it.

**Unsettled:** When the pair merges to `main` / `master` is not decided. Until then
provisioning, which clones `main`, sees none of the supervisor code, which suits the stages
before a unit install.

**Implications:** Both integration branches must be kept current with their trunks by merging
`master` / `main` into them. A MAST_common change the supervisor needs goes into common's
`supervision-integration`, not `master`.
