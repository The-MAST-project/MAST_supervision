---
decided: 2026-10-11
status: accepted
areas:
  - process-supervision
  - source-layout
---

# The launcher allows `controlled` for unit and refuses it for spec

**Why:** `plans/supervisor-design.md` §15 had the launcher refuse `controlled`, loudly, "until
MAST_unit's opmode stage 3 lands", because the app then ran `startup()` unconditionally and a
supervisor-launched app would move hardware on boot. MAST_unit#297 (merged 2026-10-06) is the
unit side of that stage: `Unit.start_lifespan` calls `startup()` only under `operated`, and under
`controlled` sets `OpState.INITIALIZED` and waits for the `startup` endpoint. It has not run on
sky. Its PR leaves out the `tested` mode and its CI smoke job, which do not bear on boot safety.
MAST_spec reads no `opmode` at all, as of 2026-10-11.

**What:** `launcher.build_launch(role, opmode, top, conf, env) -> LaunchSpec`, pure, per §8:

- **`operated`** launches VSCode on `<top>/mast-<role>.code-workspace`, `supervised=False`, with
  the environment unchanged. No VSCode found is a `LaunchError`.
- **`controlled`** launches `<python> <top>/<role>/src/app.py` from `<top>/<role>/src`,
  `supervised=True`, **for the roles in `CONTROLLED_ROLES`, which is `unit` alone**. For spec it
  raises `LaunchError`. Decided by Eli on 2026-10-11.
- The app's environment drops `http_proxy` and `https_proxy` **ignoring case**, since Windows'
  `os.environ` reports them upper-cased and a plain dict copy keeps that case. It adds
  `PYTHONUNBUFFERED=1` and passes `MAST_OPMODE` through only when the supervisor inherited it.
- A role with no app (`control`) is a `LaunchError` under either mode.
- `LaunchSettings` is a local dataclass carrying the interpreter, the located VSCode and
  `VSCodeConfig.workspace`, so the launcher imports nothing from `common.config`.

`locate.py` finds VSCode and PWI4 from MAST_provisioning's `provide-desktop-shortcuts.ps1`
candidate lists, and ps3cli and its catalog by MAST_unit's `ps3cli_locate.py` search. Both are
copies (MAST_supervision#2 row 5). PHD2 is not located, since the supervisor only observes it.
`python -m supervision --dry-run` prints the role, `opmode`, the programs with their
executables, and the launch. It exits 1 when the launch is refused or when the workspace or
`app.py` it would open does not exist, because `Code.exe` opens an empty editor on a missing
workspace without complaint. Without `--dry-run` it refuses to run until stage 3.

**Rejected:**

- *Refuse `controlled` for both roles until #297 has run on sky.* Nothing installs before
  stage 5, so the on-sky check fits before any deployment, and a later PR to lift the refusal
  would have bought nothing.
- *Allow it for both.* A spec app would ignore `controlled` and start its hardware while the
  supervisor reported it waiting. That is the silent mismatch the refusal exists to prevent.

**Unsettled:**

- `ManagedProcess` spawns with `argv` alone and runs it from the executable's folder. The app
  needs `LaunchSpec.cwd` and `LaunchSpec.env`, so stage 3 has to carry them through `ProcessApi.spawn`.
- MAST_unit's `.vscode/launch.json` "MAST unit" configuration prepends a libusb DLL folder under
  the pre-migration `PycharmProjects\MAST_unit.2024-12-12` path to `PATH`. A supervisor-launched
  app does not get it. Which component needs it, and whether that path still exists on the
  units, was not checked.
- **Under `operated` the proxy is still the app's to drop.** VSCode, and so an app started
  with F5, keeps `http_proxy`. Only MAST_unit's `start_supporting_processes()` removes it there,
  and design §9 deletes that function in stage 6. Stage 6 has to keep the deletion in the app.
  Stripping it for VSCode would not be enough, since VSCode opened from the desktop shortcut
  never passes through the supervisor.
- `--dry-run` has not run on a unit. `common.config` does not import on macOS.

**Implications:** §15's "the launcher refuses `controlled`" becomes per role in the plan. The
spec's refusal is lifted when MAST_spec mirrors opmode stages 2 and 3 (opmode-design §8 stage 6),
by adding `spec` to `CONTROLLED_ROLES`.
