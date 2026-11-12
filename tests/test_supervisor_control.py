import json
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI

from common.const import Const
from supervision.api import PROGRAM_ACTION_PATH, StatusServer, build_action_app, build_app
from supervision.instance import LOOPBACK, bind_listening
from supervision.state import ControlAnswer, Phase, ProgramAction, SupervisorSnapshot

T0 = datetime(2026, 11, 12, 18, 0, tzinfo=UTC)
NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))
TIMEOUT = 5.0


def snapshot() -> SupervisorSnapshot:
    return SupervisorSnapshot(
        taken_at=T0, started_at=T0, phase=Phase.RUNNING, hostname="mast06", role="unit", heartbeat_seconds=300.0
    )


@dataclass
class FakeControl:
    answer: ControlAnswer = ControlAnswer.ACCEPTED
    requests: list[tuple[str, ProgramAction, str]] = field(default_factory=list)

    def request_program(self, name: str, action: ProgramAction, requested_by: str) -> ControlAnswer:
        self.requests.append((name, action, requested_by))
        return self.answer


Serve = Callable[[FastAPI], int]


@pytest.fixture
def serve() -> Iterator[Serve]:
    servers: list[StatusServer] = []

    def start(app: FastAPI) -> int:
        sock = bind_listening(0, LOOPBACK)
        server = StatusServer(app, sock)
        server.start()
        servers.append(server)
        return sock.getsockname()[1]

    yield start
    for server in servers:
        server.stop()


def put(port: int, path: str) -> tuple[int, dict]:
    request = urllib.request.Request(f"http://{LOOPBACK}:{port}{path}", method="PUT")
    try:
        with NO_PROXY.open(request, timeout=TIMEOUT) as reply:
            return reply.status, json.loads(reply.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def path(name: str, action: str) -> str:
    return PROGRAM_ACTION_PATH.format(name=name, action=action)


def test_the_routes_are_under_the_supervisor_base_path() -> None:
    assert path("pwi4", "stop") == f"{Const.BASE_SUPERVISOR_PATH}/programs/pwi4/stop"


@pytest.mark.parametrize("action", list(ProgramAction))
def test_the_action_listener_serves_the_routes(serve: Serve, action: ProgramAction) -> None:
    control = FakeControl()
    port = serve(build_action_app(snapshot, None, control))

    code, _ = put(port, path("pwi4", action) + "?requested_by=operator")

    assert code == 202
    assert control.requests == [("pwi4", action, "operator")]


@pytest.mark.parametrize("action", list(ProgramAction))
def test_the_status_listener_does_not_serve_the_routes(serve: Serve, action: ProgramAction) -> None:
    port = serve(build_app(snapshot))

    code, _ = put(port, path("pwi4", action))

    assert code in {404, 405}


def test_without_a_control_the_action_listener_serves_no_routes(serve: Serve) -> None:
    port = serve(build_action_app(snapshot, None))

    code, _ = put(port, path("pwi4", "stop"))

    assert code in {404, 405}


@pytest.mark.parametrize(("answer", "code"), [(ControlAnswer.UNKNOWN, 404), (ControlAnswer.NOT_SUPERVISED, 409)])
def test_a_refused_request_says_why(serve: Serve, answer: ControlAnswer, code: int) -> None:
    port = serve(build_action_app(snapshot, None, FakeControl(answer)))

    got, body = put(port, path("phd2", "stop"))

    assert got == code
    assert body["errors"]


def test_an_action_that_is_not_stop_or_start_is_refused(serve: Serve) -> None:
    control = FakeControl()
    port = serve(build_action_app(snapshot, None, control))

    code, _ = put(port, path("pwi4", "restart"))

    assert code == 422
    assert control.requests == []
