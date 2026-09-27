"""The machine preconditions the supervisor waits on: the network, the RAM disk and its
astrometry indexes, and the share.

**No resource is hard-required.** Each is waited on for a budget; when it expires the
supervisor proceeds degraded and keeps probing. ``Config`` has a boot cache, ``Filer``
falls back to ``C:/MAST/``, and a unit with no indexes can still point, expose and guide --
one missing mount must not cost a whole night.

**The network probe is "can reach the config database"**: the controller's name resolves
and its MongoDB port accepts a connection. So a passing probe followed by a failing
``Config()`` is impossible by construction. **ICMP is rejected** -- raw ICMP needs
administrator, which the supervisor is not, and a ping answers a question nobody asked.

Every probe is bounded in time. ``is_accessible`` guards the SMB and RAM-disk paths;
the network probe runs on its own thread because ``gethostbyname`` takes no timeout and
blocks for many seconds when DNS is down. A probe abandoned at its budget finishes on its
daemon thread when the resolver gives up.

This module reads no configuration: the caller passes the controller's name and the index
set, so nothing here imports ``common.config``.

Design: mast-claude-config ``plans/supervisor-design.md`` §5.
"""

from __future__ import annotations

import socket
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PureWindowsPath
from threading import Thread

from common.filer import Filer, is_accessible, is_windows_drive_mapped
from common.mast_logging import get_logger
from supervision.clock import Clock
from supervision.state import ResourceState, ResourceStatus

logger = get_logger(__name__)

CONNECT_TIMEOUT_SECONDS = 2.0
NETWORK_ANSWER_WITHIN_SECONDS = 5.0  # resolution plus connection
ACCESS_TIMEOUT_SECONDS = 2.0


class Resource(StrEnum):
    NETWORK = "network"
    RAMDISK = "ramdisk"
    SHARE = "share"


@dataclass(frozen=True, slots=True)
class ProbeResult:
    state: ResourceState
    detail: str = ""


Probe = Callable[[], ProbeResult]


def probe_network(
    fqdn: str,
    port: int,
    connect_timeout: float = CONNECT_TIMEOUT_SECONDS,
    answer_within: float = NETWORK_ANSWER_WITHIN_SECONDS,
) -> ProbeResult:
    """Up when ``fqdn`` resolves and ``fqdn:port`` accepts a TCP connection."""
    outcome: list[ProbeResult] = []

    def _check() -> None:
        try:
            socket.gethostbyname(fqdn)
        except OSError as e:
            outcome.append(ProbeResult(ResourceState.DOWN, f"{fqdn} does not resolve: {e}"))
            return
        try:
            with socket.create_connection((fqdn, port), timeout=connect_timeout):
                pass
        except OSError as e:
            outcome.append(ProbeResult(ResourceState.DOWN, f"{fqdn}:{port} refuses or does not answer: {e}"))
            return
        outcome.append(ProbeResult(ResourceState.OK, f"{fqdn}:{port}"))

    thread = Thread(target=_check, name="supervisor-network-probe", daemon=True)
    thread.start()
    thread.join(answer_within)
    if not outcome:
        return ProbeResult(ResourceState.DOWN, f"{fqdn}:{port} gave no answer within {answer_within:g}s")
    return outcome[0]


def index_file_names(series: int, count: int) -> list[str]:
    return [f"index-{series}-{i:02d}.fits" for i in range(count)]


def missing_indexes(index_dir: Path, series: int, count: int) -> list[str]:
    """The index files absent from ``index_dir`` or empty -- what a half-finished copy leaves."""
    missing = []
    for name in index_file_names(series, count):
        path = index_dir / name
        if not path.is_file() or path.stat().st_size == 0:
            missing.append(name)
    return missing


def probe_ramdisk(index_dir: str, series: int, count: int) -> ProbeResult:
    """Down without the drive or the index folder; degraded, not down, with indexes missing.

    The drive is the one ``index_dir`` names. Windows only, as the RAM disk is.
    """
    drive = PureWindowsPath(index_dir).drive
    if not is_windows_drive_mapped(drive):
        return ProbeResult(ResourceState.DOWN, f"{drive} is not mapped")
    if not is_accessible(index_dir, ACCESS_TIMEOUT_SECONDS):
        return ProbeResult(ResourceState.DOWN, f"{index_dir} is not reachable")
    missing = missing_indexes(Path(index_dir), series, count)
    if missing:
        return ProbeResult(ResourceState.DEGRADED, f"{len(missing)} of {count} indexes missing or empty")
    return ProbeResult(ResourceState.OK, f"{count} indexes")


def probe_share(filer_factory: Callable[[], Filer] = Filer) -> ProbeResult:
    """Up when the share is reachable and this machine's product root on it is usable.

    A fresh ``Filer`` each time: one built while ``Z:`` was unmapped points ``shared`` at
    its ``C:/MAST/`` fallback for good, so it would never see the share come up. And
    ``share_root`` is probed before ``ensure_shared_root``, because that fallback is
    itself accessible -- ``ensure_shared_root`` alone reports an unmapped share as usable.
    """
    filer = filer_factory()
    if not is_accessible(filer.share_root.root, ACCESS_TIMEOUT_SECONDS):
        return ProbeResult(ResourceState.DOWN, f"{filer.share_root.root} is not reachable")
    if filer.ensure_shared_root():
        return ProbeResult(ResourceState.OK, filer.shared.root)
    return ProbeResult(ResourceState.DOWN, f"{filer.shared.root} is not usable")


class WaitOutcome(StrEnum):
    WAITING = "waiting"
    READY = "ready"  # nothing down; a degraded resource counts as available
    BUDGET_EXPIRED = "budget_expired"  # proceed degraded, and keep probing


class ResourceTracker:
    """Runs the probes, keeps each resource's state and since-when, and decides the wait.

    A probe that raises is ``down`` and never propagates. A state change is logged once,
    at the new state's severity, not on every poll.
    """

    def __init__(self, probes: Mapping[Resource, Probe], wait_budget_seconds: float, clock: Clock) -> None:
        self._probes = dict(probes)
        self._wait_budget_seconds = wait_budget_seconds
        self._clock = clock
        self._started = clock.monotonic()
        self._statuses: dict[Resource, ResourceStatus] = {}

    @property
    def statuses(self) -> tuple[ResourceStatus, ...]:
        return tuple(self._statuses.values())

    def poll(self) -> tuple[ResourceStatus, ...]:
        for resource, probe in self._probes.items():
            self._record(resource, self._run(resource, probe))
        return self.statuses

    def outcome(self) -> WaitOutcome:
        polled = len(self._statuses) == len(self._probes)
        if polled and all(s.state is not ResourceState.DOWN for s in self._statuses.values()):
            return WaitOutcome.READY
        if self._clock.monotonic() - self._started >= self._wait_budget_seconds:
            return WaitOutcome.BUDGET_EXPIRED
        return WaitOutcome.WAITING

    @staticmethod
    def _run(resource: Resource, probe: Probe) -> ProbeResult:
        try:
            return probe()
        except Exception as e:  # noqa: BLE001 -- a failing probe is a down resource, never a dead supervisor
            logger.debug(f"resource {resource}: probe raised", exc_info=True)
            return ProbeResult(ResourceState.DOWN, f"probe raised {type(e).__name__}: {e}")

    def _record(self, resource: Resource, result: ProbeResult) -> None:
        previous = self._statuses.get(resource)
        if previous is not None and previous.state is result.state:
            since = previous.since
        else:
            since = self._clock.wall()
            logger.log(result.state.severity, f"resource {resource}: {result.state} ({result.detail})")
        self._statuses[resource] = ResourceStatus(name=resource, state=result.state, detail=result.detail, since=since)
