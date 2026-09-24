# Decision records

One file per decision, dated and slugged. This repo uses the format MAST_provisioning
defined in its `docs/decisions/README.md`, which is the **canonical spec**; this file is the
working summary, and where the two disagree the MAST_provisioning spec wins.

The realistic reader is a coding agent answering *"why does this code work this way?"*
mid-task. So volume is not a cost — write a record whenever a change embodied a judgment
call — and retrievability is the whole cost.

## Naming

    docs/decisions/YYYY-MM-DD-short-slug.md

The date is the day the decision was **made**, from a real `date` invocation. Not numbered:
sequential numbers make concurrent branches race for the next integer.

## Frontmatter

A YAML block, then an H1 stating the decision as a claim, not a topic.

```yaml
---
decided: 2026-09-24
status: accepted          # proposed | accepted | superseded
issue: MAST_supervision#12  # required while proposed
areas:
  - source-layout
superseded_by:            # slug, when status: superseded
supersedes:               # slug(s) this record replaces
---
```

- **`areas`**: two to four subjects, reused from `AREAS.md` where one fits, coined and added
  there in the same PR where none does. **Block list only**, and no paths, symbols or hashes
  — those go in the prose.
- **`status`**: opened `proposed` with the PR, flipped to `accepted` in the PR that lands
  the change.

## Body

```markdown
**Why:** the problem, in the words used at the time.
**What:** the change, concretely, naming files and symbols.
**Rejected:** alternatives not taken, each with its reason.
**Unsettled:** what was not known, left unhandled, or assumed without verification.
**Implications:** consequences and follow-on work.
```

Write beliefs as beliefs, attributed to the moment (*"PHD2 was understood to…"*), not as
timeless fact. Name identifiers in the prose, spelled as in the code — that is what
`git grep` finds. Standing rules belong in `CLAUDE.md`, citing the record.

## Immutability

A record **freezes when it becomes `accepted`**. After that only `status:`,
`superseded_by:` and `areas:` may change. A reversal is a new record with `supersedes:`,
flipping the old one to `superseded` in the same PR. While `proposed`, a record is rewritten
in place — one record stating where the decision ended up.

## Across repos

A record lands in the repo whose code it decides; a decision spanning repos lives whole in
the repo carrying the larger share, with a short stub in each other repo that has adopted
this format.

## Retrieval

    git grep -il '<symbol or path>' docs/decisions/
    git grep -l '^  - <area>$' docs/decisions/
    ls docs/decisions/2*.md
    grep -l 'status: proposed' docs/decisions/2*.md
