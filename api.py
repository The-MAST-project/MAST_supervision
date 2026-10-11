"""``GET /status``: the supervisor's published snapshot over HTTP.

The route serves :class:`~supervision.state.SupervisorSnapshot` under a ``CanonicalResponse``'s
``value``, from ``STARTING`` on: a machine still waiting for its network or its configuration
is the one whose status matters most. The port and path are ``common.const.Const`` constants,
never a ``services`` row, for the same reason.

**Read-only, and the binding enforces it.** The supervisor is a console at the telescope, not a
remote-control surface. The read route binds ``0.0.0.0``; any action route added later
(``POST /opmode``, a restart) binds ``127.0.0.1`` only, so a physical event can never be the
silent consequence of a remote request.

The server runs on a socket :func:`supervision.instance.claim_port` has already bound and set
listening, so a request made before uvicorn starts waits in the backlog rather than being
refused. The access log is off: a fleet poller would add a line every few seconds.

Design: mast-claude-config ``plans/supervisor-design.md`` §7.
"""

from __future__ import annotations

import socket
import threading
from collections.abc import Callable

import uvicorn
from fastapi import FastAPI

from common.canonical import CanonicalResponse
from common.const import Const
from supervision.state import SupervisorSnapshot

STATUS_PATH = f"{Const.BASE_SUPERVISOR_PATH}/status"
SERVER_STOP_SECONDS = 5.0


def build_app(snapshot: Callable[[], SupervisorSnapshot]) -> FastAPI:
    """``snapshot`` returns the latest published snapshot; it is read once per request."""
    app = FastAPI(title="mast-supervisor")

    @app.get(STATUS_PATH)
    def status() -> CanonicalResponse:
        return CanonicalResponse(value=snapshot().model_dump(mode="json"))

    return app


class StatusServer:
    """uvicorn on a daemon thread, serving ``app`` on an already listening socket."""

    def __init__(self, app: FastAPI, sock: socket.socket) -> None:
        self._sock = sock
        # log_config=None: uvicorn's loggers propagate to the supervisor's handlers instead of
        # replacing them. uvicorn installs no signal handlers off the main thread.
        self._server = uvicorn.Server(uvicorn.Config(app, log_config=None, access_log=False))
        self._thread = threading.Thread(target=self._server.run, kwargs={"sockets": [sock]}, name="status-api", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(SERVER_STOP_SECONDS)
        self._sock.close()
