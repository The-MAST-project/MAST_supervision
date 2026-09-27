---
decided: 2026-09-27
status: accepted
areas:
  - status-reporting
  - resources
---

# Resource waits run on a monotonic clock, every probe is bounded in time, and the share probe does not trust the Filer fallback

**Why:** The supervisor waits for the network, the RAM disk and the share at boot, each for a
budget (default 300 s) after which it proceeds degraded (`plans/supervisor-design.md` §5).
Boot is exactly when NTP steps the wall clock, so a budget measured on `datetime.now()`
could expire at once or never. And the probes run one after another in one loop: a probe
that blocks stalls the others and the snapshot with them. `is_accessible` already bounds
the SMB and RAM-disk paths, but `socket.gethostbyname` takes no timeout and was understood
to block for many seconds when DNS is down.

**What:** `clock.py` holds `Clock(monotonic, wall)`, both injected. `ResourceTracker` measures
its wait budget on `monotonic` and stamps `ResourceStatus.since` from `wall`. `probe_network`
runs resolution and the TCP connect on a daemon thread and returns `down` if no answer comes
within `NETWORK_ANSWER_WITHIN_SECONDS` (5 s). "Ready" is: every resource polled at least
once and none `down`; a `degraded` RAM disk (indexes missing) counts as available, as §5's
"amber, not fatal" says. Agreed with Eli on 2026-09-27.

`probe_share` departs from §5's "`Filer().ensure_shared_root()` returns True" in two ways,
after review found that check reports an unmapped share as usable. On Windows a `Filer` built
while `Z:` is unmapped sets `shared` to its `C:/MAST/` fallback, which is accessible, so
`ensure_shared_root()` returns True at its first line; and that `Filer` never looks again.
So the probe takes a factory (default `Filer`) and builds a fresh one each time, and probes
`share_root` (`Z:/MAST/`, which has no fallback) before calling `ensure_shared_root()`, whose
creation of a missing per-machine root it keeps.

**Rejected:**

- *One wall clock for everything* — the NTP step at boot.
- *`socket.setdefaulttimeout`* — it does not bound `gethostbyname`, and it is process-wide.

**Unsettled:** Whether the `shared` fallback's effect on MAST_common's own callers is
intended was not asked: `move_ram_to_shared` and the sweeper gate on `ensure_shared_root()`,
so a unit whose `Filer` was built before `Z:` mounted moves products to `C:/MAST/`. An abandoned network probe keeps its thread until the resolver gives up. At a
5 s poll interval and a resolver timeout of the order of 10-15 s, a few such threads can
coexist; that was judged harmless, not measured.

**Implications:** `managed.py` takes the same `Clock` for its backoff and crash-loop window.
