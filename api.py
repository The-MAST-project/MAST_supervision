"""``GET /status``: the supervisor's published snapshot over HTTP; ``PUT /ramdisk/restart`` on localhost.

The route serves :class:`~supervision.state.SupervisorSnapshot` under a ``CanonicalResponse``'s
``value``, from ``STARTING`` on: a machine still waiting for its network or its configuration
is the one whose status matters most. The port and path are ``common.const.Const`` constants,
never a ``services`` row, for the same reason.

**Read-only, and the binding enforces it.** The supervisor is a console at the telescope, not a
remote-control surface. The read route binds ``0.0.0.0``; any action route (``PUT /ramdisk/restart``,
a later ``POST /opmode``) is served by :func:`build_action_app` on its own listener, bound to
``127.0.0.1:SUPERVISOR_ACTIONS_PORT``, so a physical event can never be the silent consequence of a
remote request. The action listener serves ``GET /status`` too, so a successor claiming either
port reads its holder the same way.

Both servers run on sockets :func:`supervision.instance.claim_listeners` has already bound and set
listening, so a request made before uvicorn starts waits in the backlog rather than being
refused. The access log is off: a fleet poller would add a line every few seconds.

Design: mast-claude-config ``plans/supervisor-design.md`` §7.
"""

from __future__ import annotations

import socket
import threading
from collections.abc import Callable
from http import HTTPStatus

import uvicorn
from fastapi import FastAPI, Response

from common.canonical import CanonicalResponse
from common.const import Const
from supervision.ramdisk import Answer, RamdiskReviver
from supervision.state import SupervisorSnapshot

STATUS_PATH = f"{Const.BASE_SUPERVISOR_PATH}/status"
RAMDISK_RESTART_PATH = f"{Const.BASE_SUPERVISOR_PATH}/ramdisk/restart"
SERVER_STOP_SECONDS = 5.0
UNNAMED_CLIENT = "client"

RESTART_STATUS_CODES = {
    Answer.RESTORED: HTTPStatus.OK,
    Answer.RESTORING: HTTPStatus.ACCEPTED,
    Answer.HEALTHY: HTTPStatus.CONFLICT,
    Answer.FAILED: HTTPStatus.SERVICE_UNAVAILABLE,
    Answer.GIVING_UP: HTTPStatus.SERVICE_UNAVAILABLE,
}


def build_app(snapshot: Callable[[], SupervisorSnapshot]) -> FastAPI:
    """``snapshot`` returns the latest published snapshot; it is read once per request."""
    app = FastAPI(title="mast-supervisor")

    @app.get(STATUS_PATH)
    def status() -> CanonicalResponse:
        return CanonicalResponse(value=snapshot().model_dump(mode="json"))

    return app


def build_action_app(snapshot: Callable[[], SupervisorSnapshot], reviver: RamdiskReviver | None) -> FastAPI:
    """The routes that change the machine, beside ``GET /status``. Served only on the loopback listener.

    Without a reviver (a role with no RAM disk) the listener is still claimed, and serves status only.
    """
    app = build_app(snapshot)
    if reviver is None:
        return app

    @app.put(RAMDISK_RESTART_PATH)
    def restart_ramdisk(response: Response, requested_by: str = UNNAMED_CLIENT) -> CanonicalResponse:
        answer = reviver.request(requested_by)
        code = RESTART_STATUS_CODES[answer]
        response.status_code = code
        revival = reviver.status()
        body = {"answer": answer, "revival": revival.model_dump(mode="json") if revival else None}
        if code is HTTPStatus.SERVICE_UNAVAILABLE:
            detail = revival.detail if revival else answer
            return CanonicalResponse(errors=[f"{answer}: {detail}"])
        return CanonicalResponse(value=body)

    return app


def start_servers(
    status_sock: socket.socket,
    action_sock: socket.socket,
    snapshot: Callable[[], SupervisorSnapshot],
    reviver: RamdiskReviver | None,
) -> list[StatusServer]:
    """Both servers started, listed in the order to stop them: the action listener first, so that by
    the time the status port is free for a successor, the action port is free too."""
    servers = [
        StatusServer(build_action_app(snapshot, reviver), action_sock, name="action-api"),
        StatusServer(build_app(snapshot), status_sock),
    ]
    for server in servers:
        server.start()
    return servers


class StatusServer:
    """uvicorn on a daemon thread, serving ``app`` on an already listening socket."""

    def __init__(self, app: FastAPI, sock: socket.socket, name: str = "status-api") -> None:
        self._sock = sock
        # log_config=None: uvicorn's loggers propagate to the supervisor's handlers instead of
        # replacing them. uvicorn installs no signal handlers off the main thread.
        self._server = uvicorn.Server(uvicorn.Config(app, log_config=None, access_log=False))
        self._thread = threading.Thread(target=self._server.run, kwargs={"sockets": [sock]}, name=name, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(SERVER_STOP_SECONDS)
        self._sock.close()
