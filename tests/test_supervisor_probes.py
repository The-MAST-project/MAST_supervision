import json
import socket
import socketserver
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from supervision.probes import (
    PHD2_REQUEST_ID,
    HealthResult,
    ShutdownOutcome,
    may_have_covers_open,
    parse_pwi4_status,
    probe_app,
    probe_phd2,
    probe_ps3cli,
    probe_pwi4,
    pwi4_status_is_viable,
    shut_down_if_covers_may_be_open,
    shutdown_app,
)
from supervision.state import OpState

HOST = "127.0.0.1"
SHORT = 0.3
REFUSAL_WAIT = 5.0  # Windows refuses a closed local port only after about 2 s
POLL_SECONDS = 0.02  # serve_forever polls for shutdown at this interval
PWI4_OK = (
    "pwi4.version=4.1.6\n"
    "pwi4.version_field[0]=4\n"
    "pwi4.version_field[1]=1\n"
    "pwi4.version_field[2]=6\n"
    "mirrorcover.overall_state_name=Closed\n"
)


@contextmanager
def http_server(status: int, body: bytes, delay: float = 0.0) -> Iterator[int]:
    """A local HTTP server answering every GET with ``status`` and ``body``; yields its port."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 -- http.server's name
            time.sleep(delay)
            self.send_response(status)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer((HOST, 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, args=(POLL_SECONDS,), daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


@contextmanager
def tcp_server(handle: Callable[[socket.socket], None]) -> Iterator[int]:
    """A local TCP server running ``handle`` on each connection; yields its port."""

    class Handler(socketserver.BaseRequestHandler):
        def handle(self) -> None:
            handle(self.request)

    server = socketserver.ThreadingTCPServer((HOST, 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, args=(POLL_SECONDS,), daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


def closed_port() -> int:
    with socket.socket() as s:
        s.bind((HOST, 0))
        return s.getsockname()[1]


def phd2_answering(*lines: object) -> Callable[[socket.socket], None]:
    """A PHD2 that reads one request line and then writes ``lines``, each JSON-encoded."""

    def handle(sock: socket.socket) -> None:
        sock.makefile("rb").readline()
        for line in lines:
            sock.sendall((line if isinstance(line, str) else json.dumps(line)).encode() + b"\r\n")

    return handle


def silent(sock: socket.socket) -> None:
    sock.recv(1024)
    time.sleep(SHORT * 3)


# --- PWI4 -----------------------------------------------------------------------------------


def test_pwi4_status_is_parsed_as_key_value_lines() -> None:
    assert parse_pwi4_status("a=1\nb=x=y\nnoise\n") == {"a": "1", "b": "x=y"}


def test_pwi4_healthy() -> None:
    with http_server(200, PWI4_OK.encode()) as port:
        assert probe_pwi4(HOST, port) == HealthResult(True, "PWI4 4.1.6")


def test_pwi4_unhealthy_on_a_200_that_is_not_pwi4() -> None:
    with http_server(200, b"<html>hello</html>") as port:
        result = probe_pwi4(HOST, port)
    assert not result.healthy
    assert "not as PWI4" in result.detail


def test_pwi4_older_than_the_minimum() -> None:
    raw = parse_pwi4_status(PWI4_OK.replace("version_field[2]=6", "version_field[2]=5"))
    result = pwi4_status_is_viable(raw)
    assert not result.healthy
    assert "older than 4.1.6" in result.detail


def test_pwi4_without_mirror_covers() -> None:
    raw = parse_pwi4_status(PWI4_OK)
    del raw["mirrorcover.overall_state_name"]
    assert not pwi4_status_is_viable(raw).healthy


def test_pwi4_refused() -> None:
    port = closed_port()
    assert probe_pwi4(HOST, port) == HealthResult(False, f"{HOST}:{port} refused")


def test_pwi4_that_never_answers_is_bounded() -> None:
    with http_server(200, PWI4_OK.encode(), delay=SHORT * 3) as port:
        started = time.monotonic()
        result = probe_pwi4(HOST, port, timeout=SHORT)
    assert time.monotonic() - started < SHORT * 3
    assert result.detail.endswith("no answer")


def test_http_probes_ignore_the_proxy_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    with http_server(200, PWI4_OK.encode()) as port:
        assert probe_pwi4(HOST, port).healthy


# --- PHD2 -----------------------------------------------------------------------------------


def test_phd2_skips_the_connect_events_and_reports_the_state() -> None:
    events = ({"Event": "Version", "PHDVersion": "2.6.14"}, {"Event": "AppState", "State": "Stopped"})
    with tcp_server(phd2_answering(*events, {"jsonrpc": "2.0", "result": "Guiding", "id": PHD2_REQUEST_ID})) as port:
        assert probe_phd2(HOST, port) == HealthResult(True, "Guiding")


def test_phd2_unknown_state() -> None:
    with tcp_server(phd2_answering({"result": "Dancing", "id": PHD2_REQUEST_ID})) as port:
        result = probe_phd2(HOST, port)
    assert not result.healthy
    assert "'Dancing'" in result.detail


def test_phd2_reply_to_another_request_is_not_ours() -> None:
    with tcp_server(phd2_answering({"result": "Guiding", "id": PHD2_REQUEST_ID + 1})) as port:
        assert not probe_phd2(HOST, port, timeout=SHORT).healthy


@pytest.mark.parametrize("garbage", ["not json", "[1, 2]"])
def test_phd2_garbage_is_not_phd2(garbage: str) -> None:
    with tcp_server(phd2_answering(garbage)) as port:
        assert probe_phd2(HOST, port) == HealthResult(False, "answered, but not as PHD2")


def test_phd2_silence_is_bounded() -> None:
    with tcp_server(silent) as port:
        started = time.monotonic()
        result = probe_phd2(HOST, port, timeout=SHORT)
    assert time.monotonic() - started < SHORT * 3
    assert not result.healthy


def test_phd2_refused() -> None:
    assert probe_phd2(HOST, closed_port()).detail.endswith("refused")


# --- ps3cli ---------------------------------------------------------------------------------


def test_ps3cli_listening() -> None:
    with tcp_server(lambda sock: None) as port:
        assert probe_ps3cli(HOST, port).healthy


def test_ps3cli_closed_port_is_refused_not_timed_out() -> None:
    result = probe_ps3cli(HOST, closed_port())
    assert not result.healthy
    assert result.detail.endswith("refused")


# --- the app --------------------------------------------------------------------------------


def _status(value: object) -> bytes:
    return json.dumps({"api_version": "1.0", "value": value}).encode()


def test_app_healthy_reports_its_opstate() -> None:
    with http_server(200, _status({"operational": False, "opstate": "initialized"})) as port:
        assert probe_app(port, "unit", HOST) == HealthResult(True, "opstate=initialized", OpState.INITIALIZED)


def test_an_app_starting_up_says_so() -> None:
    """A startup under way still reports the opstate it started from; its activities say it runs."""
    status = _status({"opstate": "shutdown", "activities_verbal": ["StartingUp", "Exposing"]})
    with http_server(200, status) as port:
        result = probe_app(port, "unit", HOST)
    assert result == HealthResult(
        True, "opstate=shutdown, StartingUp", OpState.SHUT_DOWN, frozenset({"StartingUp", "Exposing"})
    )
    assert result.starting_up


@pytest.mark.parametrize("activities", [None, [], ["Exposing"]])
def test_an_idle_unit_lists_no_lifecycle_change(activities: list[str] | None) -> None:
    """An idle unit's ``activities_verbal`` is ``null``, not an empty list."""
    with http_server(200, _status({"opstate": "initialized", "activities_verbal": activities})) as port:
        result = probe_app(port, "unit", HOST)
    assert result.healthy
    assert not result.starting_up
    assert not result.shutting_down


@pytest.mark.parametrize(
    ("opstate", "activities", "expected"),
    [
        (OpState.STARTED_UP, (), True),
        (OpState.INITIALIZED, ("StartingUp",), True),
        (OpState.SHUT_DOWN, ("StartingUp",), True),
        (OpState.SHUT_DOWN, ("ShuttingDown",), True),
        (OpState.INITIALIZED, ("ShuttingDown",), True),
        (OpState.INITIALIZED, (), False),
        (OpState.SHUT_DOWN, (), False),
        (OpState.INITIALIZING, (), False),
        (None, (), False),
    ],
)
def test_which_apps_may_have_their_covers_open(opstate: OpState | None, activities: tuple[str, ...], expected: bool) -> None:
    assert may_have_covers_open(HealthResult(True, "", opstate, frozenset(activities))) is expected


def test_an_opstate_this_supervisor_does_not_know_is_shown_but_not_structured() -> None:
    with http_server(200, _status({"opstate": "running"})) as port:
        assert probe_app(port, "unit", HOST) == HealthResult(True, "opstate=running")


def test_app_without_opstate_is_healthy() -> None:
    with http_server(200, _status({"operational": True})) as port:
        assert probe_app(port, "spec", HOST) == HealthResult(True, "answering")


def test_app_not_operational_is_still_healthy() -> None:
    with http_server(200, _status({"operational": False})) as port:
        assert probe_app(port, "unit", HOST).healthy


def test_a_slow_app_within_the_timeout_is_healthy() -> None:
    with http_server(200, _status({}), delay=SHORT) as port:
        assert probe_app(port, "unit", HOST, timeout=SHORT * 5).healthy


def test_an_app_slower_than_the_timeout_is_not() -> None:
    with http_server(200, _status({}), delay=SHORT * 3) as port:
        result = probe_app(port, "unit", HOST, timeout=SHORT)
    assert result.detail.endswith("no answer")


@pytest.mark.parametrize(
    ("status", "body", "reason"),
    [
        (200, b'{"api_version": "1.0"}', "status has no value"),
        (200, b"<html></html>", "status is not JSON"),
        (500, b"", "500"),
    ],
)
def test_app_unhealthy(status: int, body: bytes, reason: str) -> None:
    with http_server(status, body) as port:
        result = probe_app(port, "unit", HOST)
    assert not result.healthy
    assert reason in result.detail


# --- shutting the app down ------------------------------------------------------------------


@contextmanager
def unit_app(
    shuts_down: bool = True, start: dict | None = None, after_put: list[dict] | None = None
) -> Iterator[tuple[int, list[str]]]:
    """A unit app: ``PUT .../shutdown`` is recorded and, when ``shuts_down``, its status says so next.

    ``start`` is its status before the ``PUT`` (``startedup`` by default). ``after_put``, when
    given, is the status each read after the ``PUT`` returns in turn, the last one repeating.
    Yields the port and the requests seen, as ``METHOD path``.
    """
    seen: list[str] = []
    state: dict = dict(start or {"opstate": "startedup"})
    script = list(after_put or [])
    put = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_PUT(self) -> None:  # noqa: N802 -- http.server's name
            seen.append(f"PUT {self.path}")
            put.set()
            if shuts_down and not script:
                state.update(opstate="shutdown", activities_verbal=None)
            self._answer(_status(None))

        def do_GET(self) -> None:  # noqa: N802 -- http.server's name
            seen.append(f"GET {self.path}")
            if put.is_set() and script:
                state.clear()
                state.update(script.pop(0) if len(script) > 1 else script[0])
            self._answer(_status(dict(state)))

        def _answer(self, body: bytes) -> None:
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer((HOST, 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, args=(POLL_SECONDS,), daemon=True)
    thread.start()
    try:
        yield server.server_address[1], seen
    finally:
        server.shutdown()
        server.server_close()


def test_shutdown_is_sent_and_waited_for_until_the_app_reports_it() -> None:
    with unit_app() as (port, seen):
        outcome = shutdown_app(port, "unit", HOST, wait=SHORT * 10, poll=POLL_SECONDS)

    assert outcome == ShutdownOutcome(True, "opstate=shutdown")
    assert seen[0] == "PUT /mast/api/v1/unit/shutdown"
    assert seen[1] == "GET /mast/api/v1/unit/status"


def test_a_shutdown_still_closing_the_covers_is_not_done() -> None:
    """A unit shut down earlier reports ``shutdown`` from the first read; done is no ShuttingDown left."""
    closing = {"opstate": "shutdown", "activities_verbal": ["ShuttingDown"]}
    done = {"opstate": "shutdown", "activities_verbal": None}
    start = {"opstate": "shutdown", "activities_verbal": ["StartingUp"]}
    with unit_app(start=start, after_put=[closing, closing, done]) as (port, seen):
        outcome = shutdown_app(port, "unit", HOST, wait=SHORT * 20, poll=POLL_SECONDS)

    assert outcome == ShutdownOutcome(True, "opstate=shutdown")
    assert seen.count("GET /mast/api/v1/unit/status") == 3


def test_a_shutdown_check_on_an_idle_unit_sends_nothing() -> None:
    with unit_app(start={"opstate": "initialized", "activities_verbal": None}) as (port, seen):
        assert shut_down_if_covers_may_be_open(port, "unit", None, HOST) is None

    assert seen == ["GET /mast/api/v1/unit/status"]


def test_a_shutdown_check_on_a_unit_starting_up_shuts_it_down() -> None:
    with unit_app(start={"opstate": "shutdown", "activities_verbal": ["StartingUp"]}) as (port, seen):
        outcome = shut_down_if_covers_may_be_open(port, "unit", None, HOST)

    assert outcome is not None
    assert outcome.completed
    assert outcome.detail.startswith("was opstate=shutdown, StartingUp")
    assert "PUT /mast/api/v1/unit/shutdown" in seen


def test_a_shutdown_check_that_cannot_read_the_status_falls_back_on_the_last_opstate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A status read waits on PHD2 and can time out while ``shutdown`` is still taken."""
    from supervision import probes

    real = probes.probe_app
    reads = iter([HealthResult(False, "no answer")])
    monkeypatch.setattr(probes, "probe_app", lambda *a, **k: next(reads, None) or real(*a, **k))
    with unit_app() as (port, seen):
        outcome = probes.shut_down_if_covers_may_be_open(port, "unit", OpState.STARTED_UP, HOST)

    assert outcome is not None
    assert outcome.completed
    assert outcome.detail.startswith("was last seen startedup, status now unreadable (no answer)")
    assert "PUT /mast/api/v1/unit/shutdown" in seen


@pytest.mark.parametrize("last", [OpState.INITIALIZED, OpState.SHUT_DOWN, None])
def test_a_shutdown_check_that_cannot_read_the_status_sends_nothing(last: OpState | None) -> None:
    outcome = shut_down_if_covers_may_be_open(closed_port(), "unit", last, HOST)

    assert outcome is not None
    assert not outcome.completed
    assert "no shutdown sent" in outcome.detail


def test_a_shutdown_that_does_not_complete_is_bounded() -> None:
    with unit_app(shuts_down=False) as (port, _):
        started = time.monotonic()
        outcome = shutdown_app(port, "unit", HOST, wait=SHORT, poll=POLL_SECONDS)

    assert not outcome.completed
    assert "startedup" in outcome.detail
    assert time.monotonic() - started < SHORT * 5


def test_a_shutdown_nobody_answers_is_reported_not_raised() -> None:
    outcome = shutdown_app(closed_port(), "unit", HOST, wait=REFUSAL_WAIT, poll=POLL_SECONDS)

    assert not outcome.completed
    assert outcome.detail.endswith("refused")
