import logging
import socket
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

import supervision.resources as resources
from supervision.clock import Clock
from supervision.resources import (
    ProbeResult,
    Resource,
    ResourceTracker,
    WaitOutcome,
    probe_network,
    probe_share,
)
from supervision.state import ResourceState

T0 = datetime(2026, 9, 27, 18, 0, tzinfo=UTC)
BUDGET = 300.0
UP = ProbeResult(ResourceState.OK)
DOWN = ProbeResult(ResourceState.DOWN, "unreachable")


class FakeClock:
    def __init__(self) -> None:
        self.seconds = 0.0

    def advance(self, seconds: float) -> None:
        self.seconds += seconds

    def clock(self) -> Clock:
        return Clock(monotonic=lambda: self.seconds, wall=lambda: T0 + timedelta(seconds=self.seconds))


class Switch:
    """A probe whose answer the test sets."""

    def __init__(self, result: ProbeResult) -> None:
        self.result = result

    def __call__(self) -> ProbeResult:
        return self.result


def _tracker(fake: FakeClock, **probes) -> ResourceTracker:
    return ResourceTracker({Resource(name): probe for name, probe in probes.items()}, BUDGET, fake.clock())


def test_all_up_is_ready() -> None:
    tracker = _tracker(FakeClock(), network=Switch(UP), ramdisk=Switch(UP), share=Switch(UP))
    tracker.poll()
    assert tracker.outcome() is WaitOutcome.READY


def test_nothing_is_ready_before_the_first_poll() -> None:
    assert _tracker(FakeClock(), network=Switch(UP)).outcome() is WaitOutcome.WAITING


def test_a_degraded_resource_counts_as_available() -> None:
    tracker = _tracker(FakeClock(), ramdisk=Switch(ProbeResult(ResourceState.DEGRADED, "1 of 47")))
    tracker.poll()
    assert tracker.outcome() is WaitOutcome.READY


def test_one_down_waits_then_the_budget_expires_and_it_proceeds() -> None:
    fake = FakeClock()
    tracker = _tracker(fake, network=Switch(UP), share=Switch(DOWN))
    tracker.poll()
    assert tracker.outcome() is WaitOutcome.WAITING
    fake.advance(BUDGET)
    tracker.poll()
    assert tracker.outcome() is WaitOutcome.BUDGET_EXPIRED


def test_a_recovery_after_the_budget_is_ready() -> None:
    fake = FakeClock()
    share = Switch(DOWN)
    tracker = _tracker(fake, share=share)
    fake.advance(BUDGET + 1)
    tracker.poll()
    share.result = UP
    tracker.poll()
    assert tracker.outcome() is WaitOutcome.READY


def test_a_raising_probe_is_down_and_does_not_propagate() -> None:
    def broken() -> ProbeResult:
        raise RuntimeError("boom")

    tracker = _tracker(FakeClock(), share=broken)
    (status,) = tracker.poll()
    assert status.state is ResourceState.DOWN
    assert status.detail == "probe raised RuntimeError: boom"


def test_transitions_log_once_not_per_poll(caplog: pytest.LogCaptureFixture) -> None:
    share = Switch(DOWN)
    tracker = _tracker(FakeClock(), share=share)
    with caplog.at_level(logging.INFO, logger="mast"):
        for _ in range(5):
            tracker.poll()
        share.result = UP
        for _ in range(5):
            tracker.poll()
    messages = [(r.levelno, r.getMessage()) for r in caplog.records if r.name.startswith("mast.")]
    assert messages == [
        (logging.WARNING, "resource share: down (unreachable)"),
        (logging.INFO, "resource share: ok ()"),
    ]


def test_since_is_when_the_state_began_not_the_last_poll() -> None:
    fake = FakeClock()
    share = Switch(DOWN)
    tracker = _tracker(fake, share=share)
    tracker.poll()
    fake.advance(60)
    (status,) = tracker.poll()
    assert status.since == T0
    share.result = UP
    fake.advance(60)
    (status,) = tracker.poll()
    assert status.since == T0 + timedelta(seconds=120)


def test_network_up_when_the_port_accepts() -> None:
    with socket.create_server(("127.0.0.1", 0)) as server:
        port = server.getsockname()[1]
        assert probe_network("127.0.0.1", port).state is ResourceState.OK


def test_network_down_when_the_port_refuses() -> None:
    with socket.create_server(("127.0.0.1", 0)) as server:
        port = server.getsockname()[1]
    assert probe_network("127.0.0.1", port).state is ResourceState.DOWN


def test_network_down_when_the_name_does_not_resolve(monkeypatch: pytest.MonkeyPatch) -> None:
    def nxdomain(name: str) -> str:
        raise socket.gaierror("no such host")

    monkeypatch.setattr(resources.socket, "gethostbyname", nxdomain)
    result = probe_network("mast-ns-control.example", 27017)
    assert result.state is ResourceState.DOWN
    assert "does not resolve" in result.detail


def test_a_hung_resolver_is_down_within_the_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(resources.socket, "gethostbyname", lambda name: time.sleep(1))
    started = time.monotonic()
    result = probe_network("mast-ns-control.example", 27017, answer_within=0.1)
    assert time.monotonic() - started < 0.5
    assert result.state is ResourceState.DOWN
    assert "no answer within 0.1s" in result.detail


def _filer(share_root: str, shared_root: str, usable: bool) -> SimpleNamespace:
    return SimpleNamespace(
        share_root=SimpleNamespace(root=share_root),
        shared=SimpleNamespace(root=shared_root),
        ensure_shared_root=lambda: usable,
    )


@pytest.mark.parametrize(("usable", "state"), [(True, ResourceState.OK), (False, ResourceState.DOWN)])
def test_share_follows_ensure_shared_root(tmp_path: Path, usable: bool, state: ResourceState) -> None:
    filer = _filer(str(tmp_path), str(tmp_path / "mast07"), usable)
    assert probe_share(lambda: filer).state is state


def test_an_unmapped_share_is_down_despite_the_local_fallback(tmp_path: Path) -> None:
    # Z: unmapped: Filer's `shared` falls back to C:/MAST/, where ensure_shared_root() is True.
    filer = _filer(str(tmp_path / "unmapped-Z"), str(tmp_path), usable=True)
    result = probe_share(lambda: filer)
    assert result.state is ResourceState.DOWN
    assert "unmapped-Z" in result.detail


def test_each_probe_builds_a_fresh_filer(tmp_path: Path) -> None:
    # A Filer built before Z: mounted keeps its fallback forever; each probe must look again.
    built = []

    def factory() -> SimpleNamespace:
        built.append(1)
        return _filer(str(tmp_path), str(tmp_path / "mast07"), usable=True)

    probe_share(factory)
    probe_share(factory)
    assert len(built) == 2
