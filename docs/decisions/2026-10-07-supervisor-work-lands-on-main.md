---
decided: 2026-10-07
status: accepted
areas:
  - source-layout
  - reproducibility
supersedes:
  - 2026-09-27-supervisor-work-lands-on-a-paired-integration-branch
---

# Supervisor work lands on `main`; the `supervision-integration` branch is retired

**Why:** The paired integration branch existed because stage 1 (MAST_common#130) was held off
common's `master`, and this repo's tests import it. On 2026-10-06 MAST_common#145 merged
common's `supervision-integration` into `master` on its own, carrying #130, the #144 revert and
Arie's opmode revision (`automatic` renamed `operated`, four `OpState` values). The reason for
the pairing was gone, and the plan to merge the two branches together could no longer be kept.

**What:** This PR brings `supervision-integration` into `main`, with `state.OpMode` following
the rename (`OPERATED = "operated"`), which `test_local_enum_copies_match_common` had flagged.
Later work, MAST_supervision#5 first, targets `main`. CI no longer runs on pushes to
`supervision-integration`. The step *Resolve the paired MAST_common branch* stays as it was
(head's name, then base's, then `master`), so a change that needs an unmerged common change
goes on a branch named like common's. Decided by Eli on 2026-10-07.

**Rejected:**

- *Keeping the integration branch until the first install on a unit* — nothing is left to pair
  with on common's side, and provisioning installs nothing from this repo yet, so merging to
  `main` changes no machine.

**Unsettled:** Whether common's own `supervision-integration` branch, now merged into `master`,
is deleted is common's call.

**Implications:** `main` is the trunk the stage 2 to 5 PRs build on. Provisioning, which clones
`main`, will see the supervisor code once it learns the repo (stage 1b); until then nothing
deploys it.
