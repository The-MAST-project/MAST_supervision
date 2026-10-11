---
decided: 2026-10-11
status: accepted
areas:
  - reproducibility
  - dependency management
---

# basedpyright in standard mode blocks CI, run once on the Linux leg

**Why:** the repo was gated by ruff and pytest only (MAST_supervision#3). MAST_provisioning
adopted a type checker late and had 50 findings to clear first (MAST_provisioning#87).
Measured on `main` @ `0cda596` before this change, with basedpyright 1.39.10, `pythonPlatform:
All` and `common` resolved as the sibling clone: `standard` reported 10 errors and 2 warnings,
**none in production code**. `recommended` reported 19 errors and 225 warnings, led by
`reportUnusedCallResult` (62), `reportAny` (29), `reportUnannotatedClassAttribute` (28), and
about 70 `reportUnknown*` findings, most of them MAST_common's untyped surface leaking through.

**What:**

- `pyrightconfig.json`: include `.`, `extraPaths: [".."]` for `common`, Python 3.12,
  `pythonPlatform: All`, `typeCheckingMode: standard`. `reportMissingModuleSource` is off,
  because pywin32 is installed only on Windows and the bundled stubs type-check the imports
  elsewhere.
- `basedpyright==1.39.10` in the `dev` group, MAST_provisioning's pin. The version decides what
  counts as a finding.
- A `Types` step in CI's ubuntu test leg, after the tests. That leg already has the joint
  install pyright resolves imports from. `pythonPlatform: All` covers the win32 branches, so a
  Windows run would add nothing.
- The 10 findings, all in tests, were fixed:
  - `probe_share`'s fakes are a `Filer` subclass instead of `SimpleNamespace`;
  - an unbound loop variable now has a value;
  - `log_message` matches the base class's signature;
  - the `win32process` guard test calls each entry point with arguments typed as the stubs require.

**Rejected:**

- *`recommended` mode.* About a third of its findings are MAST_common's to fix, and the rest
  are rule-selection questions (is an unused call result a finding?) that deserve their own
  evidence once `standard` is holding.
- *A separate CI job.* It would duplicate the paired-MAST_common checkout and the joint
  install for no extra coverage.
- *`types-pywin32` as a dependency.* basedpyright's bundled stubs already type the win32
  imports, which is what surfaced the guard test's argument errors.

**Unsettled:** raising the mode, or adding individual `recommended` rules such as
`reportImplicitOverride` and `reportUnusedParameter`, is open. Stage 5's `session.py` and
`service.py` will be the first production win32 code checked against the stubs.

**Implications:** `CLAUDE.md`'s *Checks before done* includes `.venv/bin/basedpyright`. A
finding is fixed, not silenced: no `# type: ignore` or `# pyright: ignore` without a reason
that names the defect in the dependency.
