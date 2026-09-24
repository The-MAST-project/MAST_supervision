# Areas in use

The working list of `areas:` terms in this repo's decision records — descriptive, not
prescriptive. Reuse a term when one fits; coin one and add it here in the same PR when none
does. Terms shared with MAST_provisioning's `AREAS.md` are spelled the same, so a query
across both clones finds both.

| Area | Roughly |
|------|---------|
| `source-layout` | Where the code lives: repo boundaries, clone layout, `mast.pth` (shared with MAST_provisioning) |
| `reproducibility` | The same result twice: pins, locks, frozen caches (shared with MAST_provisioning) |
| `dependency management` | How dependencies are declared, and how they coexist in the role's one shared venv |
| `services` | The Windows service side: NSSM, session 0, what a service may start (shared with MAST_provisioning) |

Re-derive the terms actually in use:

    grep -h '^  - ' docs/decisions/2*.md | sort | uniq -c | sort -rn
