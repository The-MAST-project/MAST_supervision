import socket
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI

from supervision.api import STATUS_PATH, StatusServer, build_app
from supervision.clock import Clock
from supervision.instance import ClaimError, Owner, Timings, bind_listening, claim_port, runs_module
from supervision.state import Phase, SupervisorSnapshot

T0 = datetime(2026, 10, 11, 18, 0, tzinfo=UTC)
HEARTBEAT = 300.0
PEER_PID = 4242
PEER_CREATED = 1_760_000_000.0
PEER = Owner(PEER_PID, PEER_CREATED, True, f"pid {PEER_PID} (python.exe)")
FAST = Timings(probe_seconds=0.5, stopping_wait_seconds=30.0, poll_seconds=1.0, kill_grace_seconds=1.0, rebind_seconds=5.0)


def peer_snapshot(phase: Phase = Phase.RUNNING, age: timedelta = timedelta(0)) -> SupervisorSnapshot:
    return SupervisorSnapshot(
        taken_at=T0 - age, started_at=T0 - age, phase=phase, hostname="mast07", role="unit", heartbeat_seconds=HEARTBEAT
    )


@dataclass
class Holder:
    """Something holding a port: a supervisor's API, or a socket that never answers."""

    sock: socket.socket
    server: StatusServer | None = None
    released: bool = False

    @property
    def port(self) -> int:
        return self.sock.getsockname()[1]

    def release(self) -> None:
        if self.released:
            return
        self.released = True
        if self.server is not None:
            self.server.stop()
        else:
            self.sock.close()


@pytest.fixture
def holders() -> Iterator[Callable[[SupervisorSnapshot | None], Holder]]:
    made: list[Holder] = []

    def hold(snapshot: SupervisorSnapshot | None) -> Holder:
        sock = bind_listening(0)
        holder = Holder(sock)
        if snapshot is not None:
            holder.server = StatusServer(build_app(lambda: snapshot), sock)
            holder.server.start()
        made.append(holder)
        return holder

    yield hold
    for holder in made:
        holder.release()


@dataclass
class FakeOwners:
    holder: Holder | None
    is_supervisor: bool = True
    deny: bool = False
    killed: list[int] = field(default_factory=list)
    # What successive owner() calls report, overriding the holder while it lasts.
    script: list[Owner | None] = field(default_factory=list)

    def owner(self, port: int) -> Owner | None:
        if self.script:
            return self.script.pop(0)
        if self.holder is None or self.holder.released:
            return None
        return Owner(PEER_PID, PEER_CREATED, self.is_supervisor, f"pid {PEER_PID} (python.exe)")

    def kill(self, owner: Owner, grace_seconds: float) -> None:
        if self.deny:
            raise ClaimError(f"cannot kill {owner.description}: access denied")
        self.killed.append(owner.pid)
        assert self.holder is not None
        self.holder.release()


@dataclass
class Time:
    """A clock that moves only when the claim sleeps."""

    now: float = 0.0
    on_sleep: Callable[[float], None] | None = None
    slept: float = 0.0

    def clock(self) -> Clock:
        return Clock(monotonic=lambda: self.now, wall=lambda: T0 + timedelta(seconds=self.now))

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        self.slept += seconds
        if self.on_sleep is not None:
            self.on_sleep(self.now)


def claim(port: int, owners: FakeOwners, time: Time | None = None) -> socket.socket:
    time = time or Time()
    return claim_port(port, owners, time.clock(), FAST, time.sleep)


def test_a_free_port_is_claimed_and_listening() -> None:
    port = bind_listening(0)
    free = port.getsockname()[1]
    port.close()

    sock = claim(free, FakeOwners(None))

    with sock, socket.create_connection(("127.0.0.1", free), timeout=1.0):
        pass


def test_a_claimed_port_cannot_be_bound_a_second_time() -> None:
    with bind_listening(0) as first, pytest.raises(OSError):
        bind_listening(first.getsockname()[1])


@pytest.mark.parametrize("phase", [Phase.RUNNING, Phase.DEGRADED, Phase.WAITING_FOR_RESOURCES])
def test_a_live_supervisor_on_the_port_aborts_this_one_and_is_left_alone(holders, phase: Phase) -> None:
    holder = holders(peer_snapshot(phase))
    owners = FakeOwners(holder)

    with pytest.raises(ClaimError, match="another supervisor is running"):
        claim(holder.port, owners)

    assert owners.killed == []


def test_a_supervisor_whose_snapshot_is_stale_is_killed_and_the_port_taken_over(holders) -> None:
    holder = holders(peer_snapshot(age=timedelta(hours=1)))
    owners = FakeOwners(holder)

    with claim(holder.port, owners):
        assert owners.killed == [PEER_PID]


def test_a_holder_that_does_not_answer_is_killed_when_it_is_a_supervisor(holders) -> None:
    holder = holders(None)
    owners = FakeOwners(holder)

    with claim(holder.port, owners):
        assert owners.killed == [PEER_PID]


def test_a_holder_that_is_not_a_supervisor_aborts_this_one_and_is_left_alone(holders) -> None:
    holder = holders(None)
    owners = FakeOwners(holder, is_supervisor=False)

    with pytest.raises(ClaimError, match="not a supervisor"):
        claim(holder.port, owners)

    assert owners.killed == []


def test_a_holder_that_cannot_be_killed_aborts_this_one(holders) -> None:
    holder = holders(None)

    with pytest.raises(ClaimError, match="cannot kill"):
        claim(holder.port, FakeOwners(holder, deny=True))


def test_a_port_that_cannot_be_bound_with_no_owner_aborts(holders) -> None:
    holder = holders(None)

    with pytest.raises(ClaimError, match="no process is listening"):
        claim(holder.port, FakeOwners(None))


def test_a_holder_gone_before_its_owner_is_looked_up_is_rebound_not_aborted(holders) -> None:
    holder = holders(None)
    owners = FakeOwners(holder)
    owners.script = [PEER, None]  # listening when probed, gone by the second lookup
    time = Time(on_sleep=lambda now: holder.release())

    with claim(holder.port, owners, time):
        assert owners.killed == []


def test_only_the_holder_that_was_probed_is_killed(holders) -> None:
    holder = holders(None)
    owners = FakeOwners(holder)
    successor = Owner(PEER_PID + 1, PEER_CREATED + 5, True, "pid 4243 (python.exe)")
    # Probed silent, then a different process by the second lookup: that one was never
    # probed, so it is not killed; the next round probes and kills the one it looked at.
    owners.script = [PEER, successor]

    with claim(holder.port, owners, Time()):
        assert owners.killed == [PEER_PID]


def test_a_peer_of_another_snapshot_schema_is_still_live() -> None:
    later_version = {
        "taken_at": T0.isoformat(),
        "heartbeat_seconds": HEARTBEAT,
        "phase": "a_phase_from_a_later_version",
        "a_field_from_a_later_version": 1,
    }
    app = FastAPI()

    @app.get(STATUS_PATH)
    def status() -> dict:
        return {"api_version": "1.0", "value": later_version}

    sock = bind_listening(0)
    server = StatusServer(app, sock)
    server.start()
    try:
        with pytest.raises(ClaimError, match="another supervisor is running"):
            claim(sock.getsockname()[1], FakeOwners(None))
    finally:
        server.stop()


def test_a_stopping_supervisor_is_waited_for_and_not_killed(holders) -> None:
    holder = holders(peer_snapshot(Phase.STOPPING))
    owners = FakeOwners(holder)
    time = Time(on_sleep=lambda now: holder.release() if now >= 3 else None)

    with claim(holder.port, owners, time):
        assert owners.killed == []
        assert time.slept == 3


def test_a_supervisor_still_stopping_after_the_wait_is_killed(holders) -> None:
    holder = holders(peer_snapshot(Phase.STOPPING))
    owners = FakeOwners(holder)
    # Its snapshot stays fresh: the fake wall clock moves with the waits, but the snapshot
    # is only stale past the heartbeat, and the wait ends well before that.
    time = Time()

    with claim(holder.port, owners, time):
        assert owners.killed == [PEER_PID]
        assert time.slept == FAST.stopping_wait_seconds


@pytest.mark.parametrize(
    ("cmdline", "expected"),
    [
        (["C:/top/.venv/Scripts/python.exe", "-m", "supervision", "--no-gui", "--no-service"], True),
        (["python", "-X", "utf8", "-m", "supervision"], True),
        (["python", "C:/top/unit/src/app.py"], False),
        (["python", "-m", "supervision.tools"], False),
        (["python", "supervision"], False),
    ],
)
def test_a_supervisor_is_recognized_by_its_module(cmdline: list[str], expected: bool) -> None:
    assert runs_module(cmdline, "supervision") is expected
