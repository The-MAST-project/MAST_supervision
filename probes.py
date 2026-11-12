"""Health probes for the programs the supervisor owns or watches: PWI4, PHD2, ps3cli and the app.

Each probe is one bounded call against a local port, returning a :class:`HealthResult`. A
probe turns the failures it expects -- refused, timed out, an answer it cannot read -- into
``healthy=False`` with a reason; anything else propagates to the caller, which treats a
raising probe as a failed one. Thresholds, restarts and the mapping to ``ProcessState`` are
``managed.py``'s, not this module's.

**No client of the supervised programs is imported.** ``pwi4_client.PWI4().status()``
raises ``KeyError`` on any 200 that is not PWI4, and ``unit.phd2`` is the connector whose
spawning the supervisor exists to replace. The checks are re-expressed here instead, and the
ports, the minimum PWI4 version and the PHD2 app states are copies -- MAST_supervision#2.

**Timeouts are no stricter than the app's own clients**, so the supervisor never calls
unhealthy a program the app is still willing to wait for. The app's own timeout is longer
than the slowest legitimate ``status`` reply: ``Unit.status()`` reads PHD2 twice over RPC
(30 s each) and PWI4 several times (10 s each), one after another.

**HTTP bypasses any proxy.** The units carry ``http_proxy`` in their environment, and
``urllib`` would send a request for ``127.0.0.1`` through it.

**One call here acts rather than probes**: :func:`shutdown_app`, which a teardown sends a
started-up unit app before stopping it. A ``TerminateProcess`` skips the app's own shutdown, so
without it a started-up unit would be left with its covers open.

**A refused connection is reported as refused.** On the units a connect to a closed local
port is refused only after about 2 s (measured 2026-10-08 on mast01-mast08), so a connect
timeout at or below that would report "nothing listening" as "no answer".

Design: mast-claude-config ``plans/supervisor-design.md`` §6.
"""

from __future__ import annotations

import json
import socket
import time
from dataclasses import dataclass
from typing import Any
from urllib.error import URLError
from urllib.request import ProxyHandler, Request, build_opener

from common.const import Const
from supervision.state import OpState

LOCALHOST = "127.0.0.1"

PWI4_PORT = 8220
PHD2_PORT = 4400
PS3CLI_PORT = 8998

MINIMUM_PWI4_VERSION = (4, 1, 6)
PWI4_VERSION_FIELDS = 3
PWI4_COVERS_KEY = "mirrorcover.overall_state_name"

PHD2_APP_STATES = frozenset({"Stopped", "Selected", "Calibrating", "Guiding", "LostLock", "Paused", "Looping"})
PHD2_REQUEST_ID = 1

CONNECT_TIMEOUT_SECONDS = 3.0
PWI4_TIMEOUT_SECONDS = 10.0  # pwi4_client's timeout_seconds
PHD2_TIMEOUT_SECONDS = 30.0  # unit.phd2's DEFAULT_RPC_TIMEOUT
APP_STATUS_TIMEOUT_SECONDS = 70.0  # above Unit.status()'s two back-to-back PHD2 RPCs
# A unit's shutdown completes asynchronously: dispatch can wait up to 120 s for an abort, and
# MAST_unit's SHUTDOWN_SETTLE_TIMEOUT_SECONDS (70 s) counts from the end of dispatch.
APP_SHUTDOWN_WAIT_SECONDS = 200.0
APP_SHUTDOWN_POLL_SECONDS = 2.0

STATUS_PATHS = {
    "unit": f"{Const.BASE_UNIT_PATH}/status",
    "spec": f"{Const.BASE_SPEC_PATH}/status",
}
SHUTDOWN_PATHS = {"unit": f"{Const.BASE_UNIT_PATH}/shutdown"}

_opener = build_opener(ProxyHandler({}))


@dataclass(frozen=True, slots=True)
class HealthResult:
    healthy: bool
    detail: str = ""
    opstate: OpState | None = None  # the app's lifecycle state, when its status reports one this supervisor knows


@dataclass(frozen=True, slots=True)
class ShutdownOutcome:
    completed: bool  # the app reported its shutdown done within the wait
    detail: str


def _http_get(url: str, timeout: float) -> bytes:
    with _opener.open(url, timeout=timeout) as response:
        return response.read()


def _http_put(url: str, timeout: float) -> bytes:
    with _opener.open(Request(url, data=b"", method="PUT"), timeout=timeout) as response:
        return response.read()


def _describe(e: OSError) -> str:
    if isinstance(e, URLError) and isinstance(e.reason, OSError):
        e = e.reason
    if isinstance(e, ConnectionRefusedError):
        return "refused"
    if isinstance(e, TimeoutError):
        return "no answer"
    return f"{type(e).__name__}: {e}"


def parse_pwi4_status(text: str) -> dict[str, str]:
    """PWI4's ``key=value`` lines as a dict, as ``pwi4_client.status_text_to_dict`` reads them."""
    fields = (line.split("=", 1) for line in text.split("\n"))
    return {field[0]: field[1] for field in fields if len(field) == 2}


def pwi4_status_is_viable(raw: dict[str, str]) -> HealthResult:
    """``covers.pwi4_is_viable`` without the client: new enough, and ``/mirrorcover/*`` present."""
    try:
        version = tuple(int(raw[f"pwi4.version_field[{i}]"]) for i in range(PWI4_VERSION_FIELDS))
    except (KeyError, ValueError):
        return HealthResult(False, "answered, but not as PWI4")
    shown = ".".join(map(str, version))
    if version < MINIMUM_PWI4_VERSION:
        return HealthResult(False, f"PWI4 {shown}, older than {'.'.join(map(str, MINIMUM_PWI4_VERSION))}")
    if PWI4_COVERS_KEY not in raw:
        return HealthResult(False, f"PWI4 {shown} reports no {PWI4_COVERS_KEY}")
    return HealthResult(True, f"PWI4 {shown}")


def probe_pwi4(host: str = LOCALHOST, port: int = PWI4_PORT, timeout: float = PWI4_TIMEOUT_SECONDS) -> HealthResult:
    try:
        body = _http_get(f"http://{host}:{port}/status", timeout)
    except OSError as e:
        return HealthResult(False, f"{host}:{port} {_describe(e)}")
    return pwi4_status_is_viable(parse_pwi4_status(body.decode("utf-8", errors="replace")))


def _phd2_reply(sock: socket.socket, deadline: float) -> dict[str, Any] | None:
    """The reply to our request, skipping the events PHD2 sends first; ``None`` at the deadline."""
    stream = sock.makefile("rb")
    while (remaining := deadline - time.monotonic()) > 0:
        sock.settimeout(remaining)
        line = stream.readline()
        if not line:
            return None
        message = json.loads(line)
        if not isinstance(message, dict):
            raise ValueError(f"not a JSON-RPC message: {line!r}")
        if message.get("id") == PHD2_REQUEST_ID:
            return message
    return None


def probe_phd2(host: str = LOCALHOST, port: int = PHD2_PORT, timeout: float = PHD2_TIMEOUT_SECONDS) -> HealthResult:
    """One ``get_app_state`` JSON-RPC, the socket closed after it. ``detail`` is the app state."""
    request = json.dumps({"method": "get_app_state", "id": PHD2_REQUEST_ID}) + "\r\n"
    deadline = time.monotonic() + timeout
    try:
        with socket.create_connection((host, port), timeout=CONNECT_TIMEOUT_SECONDS) as sock:
            sock.sendall(request.encode())
            reply = _phd2_reply(sock, deadline)
    except OSError as e:
        return HealthResult(False, f"{host}:{port} {_describe(e)}")
    except ValueError:
        return HealthResult(False, "answered, but not as PHD2")
    if reply is None:
        return HealthResult(False, f"no get_app_state reply within {timeout:g}s")
    state = reply.get("result")
    if state not in PHD2_APP_STATES:
        return HealthResult(False, f"unknown app state {state!r}")
    return HealthResult(True, state)


def probe_ps3cli(host: str = LOCALHOST, port: int = PS3CLI_PORT) -> HealthResult:
    try:
        with socket.create_connection((host, port), timeout=CONNECT_TIMEOUT_SECONDS):
            pass
    except OSError as e:
        return HealthResult(False, f"{host}:{port} {_describe(e)}")
    return HealthResult(True, f"{host}:{port}")


def probe_app(port: int, role: str, host: str = LOCALHOST, timeout: float = APP_STATUS_TIMEOUT_SECONDS) -> HealthResult:
    """Healthy when ``status`` answers 200 with a ``value``; ``operational`` is health, not liveness.

    The app's ``opstate`` is in ``detail`` whenever its status reports one, and in ``opstate``
    when it is one this supervisor knows: a teardown decides on it.
    """
    url = f"http://{host}:{port}{STATUS_PATHS[role]}"
    try:
        body = _http_get(url, timeout)
    except OSError as e:
        return HealthResult(False, f"{url} {_describe(e)}")
    try:
        document = json.loads(body)
    except ValueError:
        return HealthResult(False, "status is not JSON")
    if not isinstance(document, dict) or "value" not in document:
        return HealthResult(False, "status has no value")
    value = document["value"]
    reported = value.get("opstate") if isinstance(value, dict) else None
    if not reported:
        return HealthResult(True, "answering")
    return HealthResult(True, f"opstate={reported}", OpState(reported) if reported in OpState else None)


def shutdown_app(
    port: int,
    role: str,
    host: str = LOCALHOST,
    wait: float = APP_SHUTDOWN_WAIT_SECONDS,
    poll: float = APP_SHUTDOWN_POLL_SECONDS,
) -> ShutdownOutcome:
    """``PUT`` the app's ``shutdown``, then read its status until it reports ``shutdown`` or ``wait`` runs out."""
    deadline = time.monotonic() + wait
    url = f"http://{host}:{port}{SHUTDOWN_PATHS[role]}"
    try:
        _http_put(url, min(wait, APP_STATUS_TIMEOUT_SECONDS))
    except OSError as e:
        return ShutdownOutcome(False, f"{url} {_describe(e)}")
    last = "no status read"
    while (remaining := deadline - time.monotonic()) > 0:
        result = probe_app(port, role, host, timeout=min(remaining, APP_STATUS_TIMEOUT_SECONDS))
        last = result.detail
        if result.opstate is OpState.SHUT_DOWN:
            return ShutdownOutcome(True, last)
        time.sleep(min(poll, max(deadline - time.monotonic(), 0.0)))
    return ShutdownOutcome(False, f"not shut down within {wait:g}s; last: {last}")
