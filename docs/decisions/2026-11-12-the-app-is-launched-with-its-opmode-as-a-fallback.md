---
decided: 2026-11-12
status: proposed
issue: MAST_common#228
areas:
  - process-supervision
---

# The app is launched with its opmode as a fallback, never as an override

**Why:** the unit app resolves its opmode alone, and fell back to `development` when it could
not find its unit (no document, or not in its site's `unit_ids`) or could not read the
configuration. Under `development` it runs the full startup. The supervisor reads the opmode
once, so an app it restarts long after boot, while the configuration cannot answer, would open
a production unit's covers with nobody there. Passing `MAST_OPMODE` would close that, but it
would break `plans/supervisor-design.md` §8: a database value would masquerade as a hand-set
override, outrank the database, and the app's log could no longer tell the two apart
(`test_mast_opmode_is_never_injected` pins the rule). Team decision, 2026-11-12.

**What:** `build_launch` sets `MAST_LAUNCH_OPMODE` in the app's environment to the opmode the
supervisor resolved. MAST_common's `resolve_opmode_with_source()` reads it only in its two
fallback branches and logs the source as "supervisor launch". `MAST_OPMODE` is still passed
through only when inherited. VSCode does not get the variable: an app the operator starts
from it was not launched by the supervisor. The name is copied into `state.py` and pinned to
`common.opmode.LAUNCH_OPMODE_ENV` by test, like the other copies (#2).

**Implications:** a readable configuration still wins, so a deliberate edit of a running
unit's document to `development` is followed at the app's next restart. Reporting that
mismatch is the supervisor's job (piece S8 of the service plan). Needs MAST_common with
`LAUNCH_OPMODE_ENV`; on an older MAST_common the app ignores the variable and the fallback is
`development`, as before.
