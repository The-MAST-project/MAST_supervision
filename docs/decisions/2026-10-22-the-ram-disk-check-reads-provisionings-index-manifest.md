---
decided: 2026-10-22
status: accepted
issue: MAST_supervision#21
areas:
  - resources
---

# The RAM-disk check reads provisioning's index manifest, and spec has no RAM-disk check

**Why:** the probe checked `D:/mast-indexes` for series 5206 x 47, `ResourcesConfig`'s defaults, copied from a commented-out block in MAST_unit. No unit holds that set: mast01, mast03 and mast05 hold 5202 and 5203, 48 files each (2026-10-22). So the resource would read `degraded` on every unit from boot, and any non-OK resource holds the phase at `degraded`. Provisioning's fileset is the canonical one, every provisioned unit holds it identically, and mast00 and mastw are exempt (team decision, 2026-10-22). MAST_provisioning#254 installs it on each unit as `C:\MAST\state\mast-indexes.json`.

**What:**

- `probe_ramdisk(index_dir, manifest)`: an unmapped drive or unreachable folder is `down`, with or without a manifest, because the revival acts on `down`. A missing manifest means the set is not checked (`ok`, "index set not checked (not provisioned)"). An unreadable one is `degraded`, quoting the error. Otherwise the folder is compared with the manifest by name and size, case-insensitively: a missing, extra or wrong-sized file is `degraded`, with the count and the first three differences.
- The manifest path and the reader are copies in this repo, per the copy-don't-consolidate rule (2026-09-24), recorded in #2 item 6. `wiring.py` no longer reads `ResourcesConfig.index_series` / `index_count`.
- The spec role has no RAM-disk resource: mast-ns-spec's `D:` is a fixed disk with no ImDisk task, and nothing there would ever read `ok`.

**Implications:**

- Names and sizes only, read on every probe: hashing about 10 GB every few seconds is not affordable. Provisioning hashes nothing either; it compares the same way.
- Removing the two dead fields from MAST_common's `ResourcesConfig` is a follow-up, safe once this has merged.

**Rejected:**

- *A reader in MAST_common, shared with MAST_unit's check (MAST_unit#115).* That check is not scheduled; the copy is recorded in #2 instead.
- *Skip the whole resource without a manifest.* A unit not yet reached by #254, and mast00 and mastw for good, would never see a lost `D:` as `down`, so it would never be revived.
