import json
import socket
import threading
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import NoReturn

import pytest

from supervision.api import RAMDISK_RESTART_PATH, STATUS_PATH, StatusServer, build_action_app, start_servers
from supervision.clock import Clock
from supervision.heartbeat import render_heartbeat
from supervision.instance import ANY_HOST, LOOPBACK, Listener, bind_listening, claim_listeners
from supervision.ramdisk import (
    RESTART_TASK,
    Answer,
    RamdiskReviver,
    RevivalSettings,
    probe_writable,
    restart_command,
)
from supervision.resources import ProbeResult
from supervision.state import Phase, ResourceState, RevivalState, Severity, SupervisorSnapshot

T0 = datetime(2026, 10, 18, 2, 14, tzinfo=UTC)
SETTINGS = RevivalSettings(
    restore_budget_seconds=60.0,
    restore_poll_seconds=2.0,
    request_wait_seconds=0.05,  # real time: only a revival left running waits it out
    short_lived_seconds=300.0,
    recent_restore_seconds=300.0,
    give_up_failures=3,
    give_up_window_seconds=3600.0,
)
READY = ProbeResult(ResourceState.OK, "47 indexes")
GONE = ProbeResult(ResourceState.DOWN, "D: is not mapped")
NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))
TIMEOUT = 5.0


class FakeClock:
    def __init__(self) -> None:
        self.seconds = 0.0

    def advance(self, seconds: float) -> None:
        self.seconds += seconds

    def clock(self) -> Clock:
        return Clock(monotonic=lambda: self.seconds, wall=lambda: T0 + timedelta(seconds=self.seconds))


class Disk:
    """A RAM disk the test kills; running the restart task brings it back after ``mount_seconds``."""

    def __init__(self, fake: FakeClock, exit_code: int = 0, mount_seconds: float = 4.0) -> None:
        self.fake = fake
        self.up = True
        self.exit_code = exit_code
        self.mount_seconds = mount_seconds
        self.mounted_at: float | None = None
        self.runs: list[list[str]] = []

    def writable(self) -> bool:
        return self.up

    def ready(self) -> ProbeResult:
        if self.mounted_at is not None and self.fake.seconds >= self.mounted_at:
            self.up = True
        return READY if self.up else GONE

    def run(self, argv: Sequence[str]) -> int:
        self.runs.append(list(argv))
        if self.exit_code == 0:
            self.mounted_at = self.fake.seconds + self.mount_seconds
        return self.exit_code

    def lose(self) -> None:
        self.up = False
        self.mounted_at = None


def inline(work: Callable[[], None]) -> None:
    work()


def reviver(fake: FakeClock, disk: Disk, start: Callable[[Callable[[], None]], None] = inline) -> RamdiskReviver:
    return RamdiskReviver(disk.writable, disk.ready, disk.run, fake.clock(), SETTINGS, sleep=fake.advance, start=start)


# --- the reviver ---------------------------------------------------------------------------------


def test_a_writable_disk_is_healthy_and_nothing_is_run() -> None:
    fake = FakeClock()
    disk = Disk(fake)
    subject = reviver(fake, disk)

    assert subject.request("unit") is Answer.HEALTHY
    assert disk.runs == []
    assert subject.status() is None


def test_a_gone_disk_is_revived_through_the_restart_task_and_reported() -> None:
    fake = FakeClock()
    disk = Disk(fake)
    subject = reviver(fake, disk)
    disk.lose()

    assert subject.request("unit") is Answer.RESTORED

    assert disk.runs == [["schtasks", "/run", "/tn", RESTART_TASK]]
    status = subject.status()
    assert status is not None
    assert status.state is RevivalState.IDLE
    assert status.lost_at == T0
    assert status.lost_at is not None and status.restored_at is not None
    assert status.restored_at > status.lost_at
    assert status.requested_by == "unit"


def test_a_task_that_does_not_run_fails_the_revival() -> None:
    fake = FakeClock()
    disk = Disk(fake, exit_code=1)
    subject = reviver(fake, disk)
    disk.lose()

    assert subject.request("unit") is Answer.FAILED
    status = subject.status()
    assert status is not None and status.failures_in_window == 1 and status.restored_at is None


def test_a_disk_not_back_within_the_budget_fails_the_revival() -> None:
    fake = FakeClock()
    disk = Disk(fake, mount_seconds=SETTINGS.restore_budget_seconds * 2)
    subject = reviver(fake, disk)
    disk.lose()

    assert subject.request("unit") is Answer.FAILED


def test_a_slow_revival_answers_restoring_and_the_next_request_joins_it() -> None:
    fake = FakeClock()
    disk = Disk(fake)
    started: list[Callable[[], None]] = []
    subject = reviver(fake, disk, start=started.append)
    disk.lose()

    assert subject.request("unit") is Answer.RESTORING
    assert subject.request("spec") is Answer.RESTORING
    status = subject.status()
    assert status is not None and status.state is RevivalState.RESTORING and status.requested_by == "unit"

    started[0]()
    assert len(started) == 1
    assert len(disk.runs) == 1
    assert subject.request("unit") is Answer.RESTORED


def test_concurrent_requests_trigger_one_revival() -> None:
    fake = FakeClock()
    disk = Disk(fake)
    release = threading.Event()
    blocking_run = disk.run

    def slow_run(argv: Sequence[str]) -> int:
        release.wait(TIMEOUT)
        return blocking_run(argv)

    settings = replace(SETTINGS, request_wait_seconds=TIMEOUT)
    subject = RamdiskReviver(disk.writable, disk.ready, slow_run, fake.clock(), settings, sleep=fake.advance)
    disk.lose()
    answers: list[Answer] = []
    threads = [threading.Thread(target=lambda: answers.append(subject.request("unit"))) for _ in range(5)]
    for thread in threads:
        thread.start()
    release.set()
    for thread in threads:
        thread.join(TIMEOUT)

    assert len(disk.runs) == 1
    assert set(answers) == {Answer.RESTORED}


def test_three_failed_revivals_within_the_window_give_up_until_the_window_passes() -> None:
    fake = FakeClock()
    disk = Disk(fake, exit_code=1)
    subject = reviver(fake, disk)
    disk.lose()

    answers = []
    for _ in range(4):
        answers.append(subject.request("unit"))
        fake.advance(60.0)

    assert answers == [Answer.FAILED, Answer.FAILED, Answer.FAILED, Answer.GIVING_UP]
    assert len(disk.runs) == 3
    status = subject.status()
    assert status is not None and status.state is RevivalState.GIVING_UP

    fake.advance(SETTINGS.give_up_window_seconds)
    assert subject.request("unit") is Answer.FAILED
    assert len(disk.runs) == 4


def test_a_disk_lost_again_soon_after_its_revival_counts_that_revival_as_failed() -> None:
    fake = FakeClock()
    disk = Disk(fake)
    subject = reviver(fake, disk)

    for _ in range(3):
        disk.lose()
        assert subject.request("unit") is Answer.RESTORED
        fake.advance(SETTINGS.short_lived_seconds / 2)
    disk.lose()

    assert subject.request("unit") is Answer.GIVING_UP


def test_a_disk_lost_long_after_its_revival_is_revived_again() -> None:
    fake = FakeClock()
    disk = Disk(fake)
    subject = reviver(fake, disk)

    for _ in range(4):
        disk.lose()
        assert subject.request("unit") is Answer.RESTORED
        fake.advance(SETTINGS.short_lived_seconds * 2)


def test_a_request_soon_after_a_revival_hears_restored_and_later_healthy() -> None:
    fake = FakeClock()
    disk = Disk(fake)
    started: list[Callable[[], None]] = []
    subject = reviver(fake, disk, start=started.append)
    disk.lose()
    assert subject.request("unit") is Answer.RESTORING
    started[0]()

    fake.advance(SETTINGS.recent_restore_seconds / 2)
    assert subject.request("unit") is Answer.RESTORED
    fake.advance(SETTINGS.recent_restore_seconds)
    assert subject.request("unit") is Answer.HEALTHY
    assert len(disk.runs) == 1


@pytest.mark.parametrize("broken", ["runner", "ready"])
def test_a_revival_that_raises_fails_and_the_next_loss_is_revived(broken: str) -> None:
    fake = FakeClock()
    disk = Disk(fake)

    def boom(*_args: object) -> NoReturn:
        raise RuntimeError("probe blew up")

    runner = boom if broken == "runner" else disk.run
    ready = boom if broken == "ready" else disk.ready
    subject = RamdiskReviver(disk.writable, ready, runner, fake.clock(), SETTINGS, sleep=fake.advance, start=inline)
    disk.lose()

    assert subject.request("unit") is Answer.FAILED
    status = subject.status()
    assert status is not None
    assert status.state is RevivalState.IDLE
    assert status.failures_in_window == 1
    assert "RuntimeError" in status.detail
    assert subject.request("unit") is Answer.FAILED


def test_giving_up_on_a_short_lived_revival_reports_the_new_loss() -> None:
    fake = FakeClock()
    disk = Disk(fake)
    subject = reviver(fake, disk)
    for _ in range(3):
        disk.lose()
        assert subject.request("unit") is Answer.RESTORED
        fake.advance(SETTINGS.short_lived_seconds / 2)
    disk.lose()
    lost = T0 + timedelta(seconds=fake.seconds)

    assert subject.request("spec") is Answer.GIVING_UP

    status = subject.status()
    assert status is not None
    assert status.lost_at == lost
    assert status.restored_at is None
    assert status.requested_by == "spec"


def test_a_probe_that_a_revival_finished_during_is_repeated_not_acted_on() -> None:
    fake = FakeClock()
    disk = Disk(fake)
    calls: list[int] = []
    subject: RamdiskReviver

    def probe() -> bool:
        calls.append(1)
        if len(calls) == 1:
            subject.request("loop")  # a revival starts and finishes while this probe is out
            return False  # what D: looked like when this probe began
        return disk.up

    subject = RamdiskReviver(probe, disk.ready, disk.run, fake.clock(), SETTINGS, sleep=fake.advance, start=inline)
    disk.lose()

    assert subject.request("unit") is Answer.RESTORED
    assert len(disk.runs) == 1
    status = subject.status()
    assert status is not None and status.failures_in_window == 0


def test_status_does_not_wait_for_a_write_probe_in_flight() -> None:
    fake = FakeClock()
    disk = Disk(fake)
    entered = threading.Event()
    release = threading.Event()

    def wedged() -> bool:
        entered.set()
        release.wait(TIMEOUT)
        return True

    subject = RamdiskReviver(wedged, disk.ready, disk.run, fake.clock(), SETTINGS, sleep=fake.advance, start=inline)
    requester = threading.Thread(target=subject.request, args=("unit",))
    requester.start()
    try:
        assert entered.wait(TIMEOUT)
        reader = threading.Thread(target=subject.status)
        reader.start()
        reader.join(TIMEOUT / 10)
        assert not reader.is_alive()
    finally:
        release.set()
        requester.join(TIMEOUT)


def test_the_restart_command_runs_the_task_and_never_imdisk_directly() -> None:
    assert restart_command() == ["schtasks", "/run", "/tn", RESTART_TASK]


def test_probe_writable_creates_and_removes_a_folder(tmp_path: Path) -> None:
    assert probe_writable(str(tmp_path))
    assert list(tmp_path.iterdir()) == []
    assert not probe_writable(str(tmp_path / "gone"))


# --- reporting -----------------------------------------------------------------------------------


def snapshot_with(subject: RamdiskReviver) -> SupervisorSnapshot:
    return SupervisorSnapshot(
        taken_at=T0,
        started_at=T0,
        phase=Phase.RUNNING,
        hostname="mast00",
        role="unit",
        heartbeat_seconds=300.0,
        ramdisk_revival=subject.status(),
    )


def test_the_heartbeat_explains_a_revival() -> None:
    fake = FakeClock()
    disk = Disk(fake, mount_seconds=58.0)
    subject = reviver(fake, disk)
    fake.advance(30.0)
    disk.lose()
    subject.request("unit")

    level, text = render_heartbeat(snapshot_with(subject))

    assert "ramdisk    lost 02:14, restored 02:15 (requested by unit)  state=idle" in text
    assert level == Severity.OK


def test_giving_up_is_an_error_in_the_snapshot() -> None:
    fake = FakeClock()
    disk = Disk(fake, exit_code=1)
    subject = reviver(fake, disk)
    disk.lose()
    for _ in range(3):
        subject.request("unit")

    assert snapshot_with(subject).severity is Severity.ERROR


# --- the route -----------------------------------------------------------------------------------


@pytest.fixture
def serve() -> Iterator[Callable[[RamdiskReviver], str]]:
    servers: list[StatusServer] = []

    def start(subject: RamdiskReviver) -> str:
        sock = bind_listening(0, LOOPBACK)
        server = StatusServer(build_action_app(lambda: snapshot_with(subject), subject), sock, name="action-api")
        server.start()
        servers.append(server)
        return f"http://{LOOPBACK}:{sock.getsockname()[1]}{RAMDISK_RESTART_PATH}"

    yield start
    for server in servers:
        server.stop()


def put(url: str) -> tuple[int, dict]:
    request = urllib.request.Request(f"{url}?requested_by=unit", method="PUT")
    try:
        with NO_PROXY.open(request, timeout=TIMEOUT) as reply:
            return reply.status, json.loads(reply.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_the_route_answers_409_for_a_healthy_disk(serve) -> None:
    fake = FakeClock()
    code, body = put(serve(reviver(fake, Disk(fake))))

    assert code == 409
    assert body["value"]["answer"] == "healthy"


def test_the_route_answers_200_once_restored(serve) -> None:
    fake = FakeClock()
    disk = Disk(fake)
    disk.lose()
    code, body = put(serve(reviver(fake, disk)))

    assert code == 200
    assert body["value"]["revival"]["requested_by"] == "unit"


def test_the_route_answers_202_while_restoring(serve) -> None:
    fake = FakeClock()
    disk = Disk(fake)
    disk.lose()
    code, body = put(serve(reviver(fake, disk, start=lambda work: None)))

    assert code == 202
    assert body["value"]["revival"]["state"] == "restoring"


def test_the_route_answers_503_when_giving_up(serve) -> None:
    fake = FakeClock()
    disk = Disk(fake, exit_code=1)
    subject = reviver(fake, disk)
    disk.lose()
    for _ in range(3):
        subject.request("unit")

    code, body = put(serve(subject))

    assert code == 503
    assert body["errors"][0].startswith("giving_up")


def non_loopback_address() -> str | None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        try:
            probe.connect(("192.0.2.1", 9))  # TEST-NET-1: nothing is sent
        except OSError:
            return None
        address = probe.getsockname()[0]
    return None if address.startswith("127.") or address == "0.0.0.0" else address  # noqa: S104


def test_the_action_listener_is_unreachable_on_a_non_loopback_address() -> None:
    address = non_loopback_address()
    if address is None:
        pytest.skip("this machine has no non-loopback IPv4 address")
    with bind_listening(0, LOOPBACK) as sock, pytest.raises(OSError):
        socket.create_connection((address, sock.getsockname()[1]), timeout=TIMEOUT).close()


@pytest.fixture
def real_servers() -> Iterator[Callable[[RamdiskReviver | None], tuple[int, int]]]:
    running: list[list[StatusServer]] = []

    def start(subject: RamdiskReviver | None) -> tuple[int, int]:
        status_sock, action_sock = claim_listeners(
            (Listener(0, ANY_HOST), Listener(0, LOOPBACK)), _NoOwners(), FakeClock().clock()
        )
        assert action_sock.getsockname()[0] == LOOPBACK
        running.append(
            start_servers(status_sock, action_sock, lambda: snapshot_with(subject) if subject else _bare(), subject)
        )
        return status_sock.getsockname()[1], action_sock.getsockname()[1]

    yield start
    for servers in running:
        for server in servers:
            server.stop()


class _NoOwners:
    def owner(self, port: int) -> None:
        return None

    def kill(self, owner: object, grace_seconds: float) -> None:
        raise AssertionError("nothing should be killed")


def _bare() -> SupervisorSnapshot:
    return SupervisorSnapshot(
        taken_at=T0, started_at=T0, phase=Phase.RUNNING, hostname="mast-ns-spec", role="spec", heartbeat_seconds=300.0
    )


def call(method: str, port: int, path: str) -> int:
    request = urllib.request.Request(f"http://{LOOPBACK}:{port}{path}", method=method)
    try:
        with NO_PROXY.open(request, timeout=TIMEOUT) as reply:
            return reply.status
    except urllib.error.HTTPError as e:
        return e.code


def test_the_status_listener_does_not_serve_the_restart_route(real_servers) -> None:
    fake = FakeClock()
    status_port, action_port = real_servers(reviver(fake, Disk(fake)))

    assert call("PUT", status_port, RAMDISK_RESTART_PATH) in {404, 405}
    assert call("PUT", action_port, RAMDISK_RESTART_PATH) == 409


def test_both_listeners_serve_status_so_the_claim_reads_either_alike(real_servers) -> None:
    fake = FakeClock()
    status_port, action_port = real_servers(reviver(fake, Disk(fake)))

    assert call("GET", status_port, STATUS_PATH) == 200
    assert call("GET", action_port, STATUS_PATH) == 200


def test_without_a_reviver_the_action_listener_serves_no_restart_route(real_servers) -> None:
    _, action_port = real_servers(None)

    assert call("PUT", action_port, RAMDISK_RESTART_PATH) in {404, 405}
    assert call("GET", action_port, STATUS_PATH) == 200


def test_a_disk_back_by_itself_while_giving_up_is_healthy() -> None:
    fake = FakeClock()
    disk = Disk(fake, exit_code=1)
    subject = reviver(fake, disk)
    disk.lose()
    for _ in range(3):
        subject.request("unit")
    disk.up = True

    assert subject.request("unit") is Answer.HEALTHY
