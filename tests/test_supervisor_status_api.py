import json
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from common.const import Const
from supervision.api import STATUS_PATH, StatusServer, build_app
from supervision.instance import bind_listening
from supervision.state import Phase, ProcessState, ProcessStatus, Severity, SupervisorSnapshot

T0 = datetime(2026, 10, 11, 18, 0, tzinfo=UTC)
PACKAGE = Path(__file__).resolve().parents[1]
NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))
TIMEOUT = 5.0


def snapshot(phase: Phase = Phase.STARTING, processes: tuple[ProcessStatus, ...] = ()) -> SupervisorSnapshot:
    return SupervisorSnapshot(
        taken_at=T0,
        started_at=T0,
        phase=phase,
        hostname="mast07",
        role="unit",
        heartbeat_seconds=300.0,
        processes=processes,
    )


Serve = Callable[[Callable[[], SupervisorSnapshot]], str]


@pytest.fixture
def serve() -> Iterator[Serve]:
    """Serve a snapshot source on an ephemeral port; returns the status URL."""
    servers: list[StatusServer] = []

    def start(source: Callable[[], SupervisorSnapshot]) -> str:
        sock = bind_listening(0)
        server = StatusServer(build_app(source), sock)
        server.start()
        servers.append(server)
        return f"http://127.0.0.1:{sock.getsockname()[1]}{STATUS_PATH}"

    yield start
    for server in servers:
        server.stop()


def get(url: str) -> dict:
    with NO_PROXY.open(url, timeout=TIMEOUT) as reply:
        return json.loads(reply.read())


def test_a_starting_supervisor_is_served_before_its_configuration_loads(serve: Serve) -> None:
    body = get(serve(lambda: snapshot(Phase.STARTING)))

    value = body["value"]
    assert value["phase"] == "starting"
    assert (value["opmode"], value["config_generation"], value["resources"]) == (None, None, [])


def test_the_body_is_a_canonical_response_carrying_the_snapshot(serve: Serve) -> None:
    body = get(serve(lambda: snapshot(Phase.RUNNING)))

    assert body["api_version"] == "1.0"
    assert body["errors"] is None
    assert SupervisorSnapshot.model_validate(body["value"]) == snapshot(Phase.RUNNING)


def test_a_not_supervised_program_is_no_fault(serve: Serve) -> None:
    vscode = ProcessStatus(name="vscode", mode=None, state=ProcessState.NOT_SUPERVISED, since=T0)

    body = get(serve(lambda: snapshot(Phase.RUNNING, (vscode,))))

    assert body["value"]["severity"] == Severity.OK


def test_each_request_serves_the_latest_snapshot(serve: Serve) -> None:
    published = [snapshot(Phase.STARTING)]
    url = serve(lambda: published[-1])
    get(url)

    published.append(snapshot(Phase.RUNNING))

    assert get(url)["value"]["phase"] == "running"


def test_the_route_is_under_the_supervisor_base_path() -> None:
    assert f"{Const.BASE_SUPERVISOR_PATH}/status" == STATUS_PATH


def test_the_route_is_read_only(serve: Serve) -> None:
    request = urllib.request.Request(serve(lambda: snapshot()), data=b"{}", method="POST")

    with pytest.raises(urllib.error.HTTPError) as refused:
        NO_PROXY.open(request, timeout=TIMEOUT)

    assert refused.value.code == 405


@pytest.mark.parametrize("module", ["api.py", "instance.py"])
def test_the_port_never_comes_from_the_configuration_database(module: str) -> None:
    source = (PACKAGE / module).read_text()

    assert "get_service" not in source
    assert "common.config" not in source


def test_stopping_the_server_frees_its_port() -> None:
    sock = bind_listening(0)
    port = sock.getsockname()[1]
    server = StatusServer(build_app(lambda: snapshot()), sock)
    server.start()

    server.stop()

    bind_listening(port).close()
