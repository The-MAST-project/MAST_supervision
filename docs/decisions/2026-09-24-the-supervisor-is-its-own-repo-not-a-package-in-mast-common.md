---
decided: 2026-09-24
status: accepted
areas:
  - source-layout
  - services
---

# The supervisor is its own repo, not a package in MAST_common

**Why:** The supervisor design (`mast-claude-config/plans/supervisor-design.md`, first round
2026-09-16) placed the code at `common/supervisor/`, to reuse MAST_common's existing clone on
every unit and spec machine, its `mast.pth` entry and its CI. On review the fit was
understood to be wrong in kind: MAST_common is a library, installed on the Linux control host
and in CI images, and the supervisor is an application — a Windows service, win32 and tkinter
bindings, a process meant to run unattended for years. Inside common it needed fences: `win32*`
confined to two files, the GUI dependency kept out of `common/requirements.txt` so it would
not reach the control host, and a refusal when `machine_role == "control"`. It would also have
shipped with every common pull made for the app's sake, including onto whatever feature
branch someone had checked out in a unit's shared `common` clone.

**What:** `mast-service` (`python -m supervision.service`) and `mast-supervisor`
(`python -m supervision`) live together in MAST_supervision, cloned by `mast-clone` as
`<top>/supervision/`. The repo root is the `supervision` package, as MAST_common's root is
`common`, so the existing `mast.pth` imports it with no new entry. What another program would
import stays in MAST_common: the `SupervisorConfig` schema in `common/config/supervisor.py`
(it sits beside `UnitConfig` and `SpecsConfig`), the port constants and `SUPERVISOR_PORT` /
`BASE_SUPERVISOR_PATH` in `common/const.py`, `common/opmode.py`, and `find_processes` /
`process_session_id` in `common/process.py`. `state.py`, the published snapshot, stays here
until a second program imports it. The two programs share one repo because they share a
protocol — the `--role` argument, the `Global\` mutex and stop-event names, and the exit
code that distinguishes restart-to-apply from a crash. Agreed with Arie on 2026-09-24.

**Rejected:**

- *`common/supervisor/`*, the first draft — for the reasons under Why. Its cost argument (a
  new repo needs a clone, a `.pth` entry, CI and a provisioning entry) was checked against
  MAST_provisioning `main` and found small: `tools/mast-repos.tsv` takes one row, and
  `mast.pth` already covers any folder under `<top>`.
- *MAST_provisioning* — it owns machine setup (NSSM, autologon) and the process-supervision
  epic MAST_provisioning#82, but it is the provisioning server's code, and the supervisor is
  runtime code on the unit.
- *MAST_unit* — the supervisor also runs on spec machines, and must exist before the app,
  not inside it.
- *Two repos, one per program* — would version the service–supervisor protocol across a
  repo boundary.

**Unsettled:**

- The name collides with an unrelated, popular PyPI package, Roboflow's `supervision`.
  `mast.pth` entries follow site-packages on `sys.path`, so installing that package into a
  fleet venv would shadow this one silently. `tests/test_package_location.py` catches it; it
  was not installed anywhere when checked.
- MAST_provisioning#159 (2026-08-30) made provisioning register no MAST service, enforced by
  `server/prov/tests/test_no_service_registration.py` and the `mast-services-finalize`
  absence check. Installing `mast-service` needs a superseding decision there; this record
  does not make it.
- MAST_common has not adopted this record format, so it carries no stub pointing here.

**Implications:** MAST_supervision is pinned independently in `mast-repos.tsv`, so the
supervisor can stay on a known-good revision while MAST_common moves. Provisioning needs the
manifest row before a unit can clone it.
