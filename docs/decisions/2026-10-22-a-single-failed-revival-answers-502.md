---
decided: 2026-10-22
status: accepted
issue: MAST_supervision#18
areas:
  - resources
  - status-reporting
---

# A single failed RAM-disk revival answers 502, giving up stays 503

**Why:** `PUT /ramdisk/restart` answered `503` both for one revival that did not bring `D:` back and for the give-up limit. MAST_common's `SupervisorApi` takes the status as the whole answer, so `Filer` logged "the supervisor has given up" after a single failure, while the supervisor kept reviving on its own probe. Recorded as unsettled in `2026-10-18-the-supervisor-revives-the-ram-disk-through-a-system-task-on-a-loopback-route.md`.

**What:** `Answer.FAILED` maps to `502 Bad Gateway`: the restart task, upstream of the route, did not do its job. `GIVING_UP` stays `503`. Both carry their detail in `errors`. MAST_common's client gains `RamdiskRestartAnswer.FAILED = 502` on the same-named branch, and sends `requested_by`, so its requests are no longer recorded as `client`.

**Implications:** this supervisor against an older client reads `502` as `NO_ANSWER` ("unexpected HTTP 502"); merge MAST_common first.

**Rejected:** keeping `503` and having the client parse the answer from the body. The client's rule is that the status is the answer.
