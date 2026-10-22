---
decided: 2026-10-18
status: accepted
issue: MAST_supervision#18
areas:
  - resources
  - status-reporting
---

# The supervisor revives the RAM disk through a SYSTEM task, and is asked to on a loopback-only route

**Why:** `D:` is an ImDisk RAM disk mounted at boot by a scheduled task, and Windows does not restart a scheduled task, so a disk that dies mid-run stays dead. The filing-areas plan (mast-claude-config `plans/filing-areas-design.md` §8.1) makes the supervisor its owner: it revives `D:` on its own probe, and a unit's `Filer` can ask it to through `PUT /ramdisk/restart`.

**What:**

- `ramdisk.py`'s `RamdiskReviver` runs one revival at a time: `schtasks /run /tn \mast-ramdisk-restart`, then it polls `probe_ramdisk` until `D:` is back, within `restore_budget_seconds`. Back means mounted with its index folder reachable (anything but `down`): missing indexes are the image's content, which another restart cannot change, and the resource check reports them. The probe's detail is kept in the report. A request first probes `D:` with a write (`probe_writable`), outside the reviver's lock, and a writable disk answers `healthy` with nothing done. A request during a revival joins it. A probe that a revival finished during is repeated rather than acted on: its "not writable" predates the restored disk, and acting on it would wipe that disk.
- A writable disk within `recent_restore_seconds` (5 minutes) of a revival answers `restored`, not `healthy`, so a caller that got `202` and asks again hears the outcome. The filing-areas plan has `Filer` act only on `restored`.
- A revival that raises counts as a failed one, and the reviver is free for the next loss.
- An outage's `lost_at` and `requested_by` are set when the disk was last known up, including when the loss is the one that tips the reviver into giving up; retries after a failed revival keep the first loss.
- After 3 failed or short-lived revivals within an hour, the reviver gives up until the oldest failure leaves the window. A revival counts as short-lived when the disk is lost again within 5 minutes.
- `Supervisor._revive_lost_ramdisk` revives without being asked, but only in `running` or `degraded`. During the resource wait, `D:` is missing at boot until the ImDisk task has run.
- `SupervisorSnapshot.ramdisk_revival` reports the last loss, the restoration and who asked. The heartbeat prints it, and `giving_up` is an ERROR.
- The route is served by `build_action_app` on its own listener, bound to `127.0.0.1:Const.SUPERVISOR_ACTION_PORT` (8005, in MAST_common). That listener also serves `GET /status`.
- **Both listeners are claimed alike, and both are required.** `instance.claim_listeners` claims `LISTENERS` in order, the status listener on `0.0.0.0:8004` and then the action listener on `127.0.0.1:8005`, each through the same `claim_port`: a live supervisor aborts this one, a stopping one is waited for, a dead one is killed and taken over, anything else aborts. If either cannot be had, the supervisor does not start, and closes what it had claimed. On shutdown the action server stops first, so a successor that finds 8004 free finds 8005 free too. Team decision, 2026-10-22.
- The reviver is wired for the `unit` role only. A spec supervisor still claims the action listener, which then serves `/status` alone.

**Implications:**

- The supervisor never calls `imdisk` itself. Measured on mast00 on 2026-10-18: UAC is on (`EnableLUA=1`), and the ImDisk task's ACL gives the `mast` user only `FR`. A non-elevated supervisor can neither detach a disk nor run that task. The SYSTEM task `\mast-ramdisk-restart`, registered by MAST_provisioning's imdisk provider with read+execute for interactive users, does both. It force-detaches `D:` and runs the boot task, so the image is still named only in the boot task.
- mast-ns-spec's `D:` is a 22 TB fixed disk with no ImDisk task (checked on 2026-10-18), so a spec supervisor has no reviver.
- A taken action port stops the supervisor from starting, as a taken status port does. Something else listening on 8005 is a fault to fix, not one to run beside.
- A client polls by asking again: `202` while restoring, then `200` for 5 minutes after the revival, then `409`.

**Rejected:**

- *Run without the action routes when 8005 is taken, logging an ERROR* (this record's first draft). It left two binding rules for two listeners, and a supervisor that cannot be asked to revive `D:` quietly running. Superseded before merge, team decision, 2026-10-22.

- *Serve the route on the 8004 listener and refuse non-loopback callers in the handler.* No new port and no MAST_common change, but the guarantee would sit in an `if` that one edit can drop. The repo's rule is that action routes are bound to loopback (`api.py`).
- *Run `imdisk -D` and `schtasks /run` from the supervisor, as the issue first described.* Neither works unelevated.
- *Stop for good after the limit.* "Within an hour" reads as a sliding window, and a revival once the window has passed costs little next to a night left on the fallback.

**Unsettled:**

- No revival has been run on hardware. The manual test is in the issue: on mast00 with the operator present, remove `D:`, ask, confirm it comes back with its indexes, then confirm a healthy disk answers `409`. mast00's boot task still runs as `mast`, not SYSTEM, because mast00 is not production-provisioned, so its restart task must be registered by hand.
- `restore_budget_seconds` (300 s) is a guess for loading the 15 GB image, and is not measured.
