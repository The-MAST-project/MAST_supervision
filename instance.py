"""One supervisor per machine, and the status port is the lock.

The supervisor binds ``SUPERVISOR_PORT`` before it does anything else, so a second one never
reaches its programs. A supervisor that started without its API, beside an earlier one that
still answers, is worse than one that failed to start: two loops would adopt and restart the
same programs, and the one nobody can see is the one in charge. So when the port is taken,
whoever holds it is asked for ``/status``:

- **A live supervisor**, whose snapshot is fresh: this one aborts, whatever that one's phase.
  A ``DEGRADED`` peer is reporting a problem a replacement would meet too.
- **A supervisor that is** ``STOPPING``: waited for, up to ``STOPPING_WAIT_SECONDS``, then
  taken over as stuck.
- **Anything else** -- no answer, a stale snapshot, a reply that is not a snapshot: the holder is
  dead or wedged. A supervisor is killed and the port taken over; its programs keep running and
  are adopted. A holder that is not a supervisor, or cannot be identified or killed, aborts this
  one instead.

Any other failure to bind aborts too. This holds a predecessor in another login session to the
same rule: never killing another session's copy covers the programs a supervisor runs, not
another supervisor.

Design: mast-claude-config ``plans/supervisor-design.md`` §7.
"""

from __future__ import annotations

import http.client
import socket
import sys
import time
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

import psutil
from pydantic import AwareDatetime, BaseModel, ConfigDict

from common.canonical import CanonicalResponse
from common.mast_logging import get_logger
from supervision.api import SERVER_STOP_SECONDS, STATUS_PATH
from supervision.clock import Clock
from supervision.state import Phase, is_stale

logger = get_logger(__name__)

ANY_HOST = "0.0.0.0"  # noqa: S104 -- the read-only status route is meant to be read fleet-wide
LOOPBACK = "127.0.0.1"
SUPERVISOR_MODULE = __name__.partition(".")[0]  # run as `python -m supervision`


class ClaimError(Exception):
    """This supervisor must not start: the port cannot be had."""


class Peer(StrEnum):
    LIVE = "live"
    STOPPING = "stopping"
    DEAD = "dead"


@dataclass(frozen=True, slots=True)
class Timings:
    probe_seconds: float = 3.0  # one /status request
    # A supervisor closes its listener right after publishing STOPPING; this covers uvicorn's
    # shutdown with room to spare. Anything still stopping past it is stuck.
    stopping_wait_seconds: float = 3 * SERVER_STOP_SECONDS
    poll_seconds: float = 1.0
    kill_grace_seconds: float = 10.0
    rebind_seconds: float = 10.0


DEFAULT_TIMINGS = Timings()


@dataclass(frozen=True, slots=True)
class Owner:
    """A process, identified by its pid and creation time so a reused pid is not mistaken for it."""

    pid: int
    created: float
    is_supervisor: bool
    description: str


class PortOwners(Protocol):
    def owner(self, port: int) -> Owner | None:
        """The process listening on ``port``, or None when none is."""
        ...

    def kill(self, owner: Owner, grace_seconds: float) -> None:
        """Terminate ``owner``, then kill it after ``grace_seconds``; raise :class:`ClaimError` if denied.

        A process that has already exited, or whose pid now names another process, is left alone.
        """
        ...


class Liveness(BaseModel):
    """What the lock reads from a peer's snapshot. Only these fields, so the rest of the snapshot's
    schema can change without a running supervisor of the previous version looking dead."""

    model_config = ConfigDict(extra="ignore")

    taken_at: AwareDatetime
    heartbeat_seconds: float
    phase: str


def claim_port(
    port: int,
    owners: PortOwners,
    clock: Clock,
    timings: Timings = DEFAULT_TIMINGS,
    sleep: Callable[[float], None] = time.sleep,
) -> socket.socket:
    """A socket bound to ``port`` on every interface and listening, or :class:`ClaimError`.

    The holder is looked up before and after it is probed, and only a holder that stayed the same
    process is killed: one that changed meanwhile, such as another supervisor that has just taken
    over, is probed afresh. A holder that is gone by the second lookup is a port to bind again.
    """
    claim = _Claim(port, owners, clock, timings, sleep)
    while True:
        try:
            return bind_listening(port)
        except OSError as e:
            holder = claim.holder_to_take_over(e)
        if holder is not None:
            return _take_over(port, holder, owners, clock, timings, sleep)


@dataclass(slots=True)
class _Deadline:
    seconds: float
    until: float | None = None


@dataclass(slots=True)
class _Claim:
    port: int
    owners: PortOwners
    clock: Clock
    timings: Timings
    sleep: Callable[[float], None]
    stopping: _Deadline = field(init=False)
    vanished: _Deadline = field(init=False)

    def __post_init__(self) -> None:
        self.stopping = _Deadline(self.timings.stopping_wait_seconds)
        self.vanished = _Deadline(self.timings.rebind_seconds)

    def holder_to_take_over(self, bind_error: OSError) -> Owner | None:
        """The holder to kill, or None to try the bind again; :class:`ClaimError` to give up."""
        probed = self.owners.owner(self.port)
        peer = fetch_liveness(self.port, self.timings.probe_seconds)
        match classify(peer, self.clock):
            case Peer.LIVE:
                raise ClaimError(f"another supervisor is running on port {self.port}: {_describe(peer)}")
            case Peer.STOPPING:
                if self._waiting(self.stopping, "the supervisor holding it is stopping"):
                    return None
            case Peer.DEAD:
                pass
        holder = self.owners.owner(self.port)
        if holder is None:
            if self._waiting(self.vanished, f"it cannot be bound ({bind_error}) and no process holds it"):
                return None
            raise ClaimError(f"cannot bind port {self.port} ({bind_error}), and no process is listening on it")
        return holder if holder == probed else None

    def _waiting(self, deadline: _Deadline, why: str) -> bool:
        """Sleep one poll and return True until ``deadline``, started on the first call, passes."""
        now = self.clock.monotonic()
        if deadline.until is None:
            deadline.until = now + deadline.seconds
            logger.info(f"port {self.port}: {why}; waiting up to {deadline.seconds:g}s")
        if now < deadline.until:
            self.sleep(self.timings.poll_seconds)
            return True
        logger.warning(f"port {self.port}: {why}, still after {deadline.seconds:g}s")
        return False


def _take_over(
    port: int, owner: Owner, owners: PortOwners, clock: Clock, timings: Timings, sleep: Callable[[float], None]
) -> socket.socket:
    if not owner.is_supervisor:
        raise ClaimError(f"port {port} is held by {owner.description}, which is not a supervisor")
    logger.warning(f"port {port} is held by a supervisor that does not answer, {owner.description}; killing it to take over")
    owners.kill(owner, timings.kill_grace_seconds)
    deadline = clock.monotonic() + timings.rebind_seconds
    while True:
        try:
            return bind_listening(port)
        except OSError as e:
            if clock.monotonic() >= deadline:
                raise ClaimError(f"killed {owner.description}, but port {port} is still not free: {e}") from e
        sleep(timings.poll_seconds)


def bind_listening(port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        # On Windows SO_REUSEADDR would let a second socket bind the same port, which is the
        # one thing the lock must refuse. On POSIX it only allows rebinding over TIME_WAIT
        # connections left by a supervisor just killed.
        if sys.platform == "win32":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((ANY_HOST, port))
        sock.listen()
    except OSError:
        sock.close()
        raise
    return sock


def fetch_liveness(port: int, timeout: float) -> Liveness | None:
    """What the snapshot served on ``port`` says of its loop, or None when nothing there answers with one."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"http://{LOOPBACK}:{port}{STATUS_PATH}", timeout=timeout) as reply:
            body = reply.read()
        return Liveness.model_validate(CanonicalResponse.model_validate_json(body).value)
    except (OSError, http.client.HTTPException, ValueError):
        return None


def classify(peer: Liveness | None, clock: Clock) -> Peer:
    if peer is None or is_stale(peer.taken_at, peer.heartbeat_seconds, clock.wall()):
        return Peer.DEAD
    return Peer.STOPPING if peer.phase == Phase.STOPPING else Peer.LIVE


def _describe(peer: Liveness | None) -> str:
    if peer is None:
        return "no snapshot"
    return f"phase {peer.phase}, last published {peer.taken_at:%Y-%m-%d %H:%M:%S}Z"


def runs_module(cmdline: Sequence[str], module: str) -> bool:
    """True when ``cmdline`` is ``python ... -m <module> ...``."""
    return any(flag == "-m" and name == module for flag, name in zip(cmdline, cmdline[1:], strict=False))


class PsutilPortOwners:
    """:class:`PortOwners` over psutil. Listing other processes' sockets needs root on macOS."""

    def owner(self, port: int) -> Owner | None:
        try:
            connections = psutil.net_connections(kind="tcp")
        except psutil.AccessDenied as e:
            raise ClaimError(f"cannot see which process holds port {port}: {e}") from e
        pid = next(
            (c.pid for c in connections if c.status == psutil.CONN_LISTEN and c.laddr and c.laddr.port == port and c.pid),
            None,
        )
        if pid is None:
            return None
        try:
            proc = psutil.Process(pid)
            created = proc.create_time()
        except psutil.NoSuchProcess:
            return None
        try:
            return Owner(pid, created, runs_module(proc.cmdline(), SUPERVISOR_MODULE), f"pid {pid} ({proc.name()})")
        except psutil.NoSuchProcess:
            return None
        except psutil.AccessDenied:
            return Owner(pid, created, False, f"pid {pid} (its command line cannot be read)")

    def kill(self, owner: Owner, grace_seconds: float) -> None:
        try:
            proc = psutil.Process(owner.pid)
            if proc.create_time() != owner.created:
                return
            proc.terminate()
            try:
                proc.wait(grace_seconds)
            except psutil.TimeoutExpired:
                proc.kill()
                proc.wait(grace_seconds)
        except psutil.NoSuchProcess:
            return
        except (psutil.AccessDenied, psutil.TimeoutExpired) as e:
            raise ClaimError(f"cannot kill {owner.description}: {e}") from e
