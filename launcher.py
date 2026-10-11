"""What the supervisor starts after the resources and the supervised programs: VSCode or the app.

:func:`build_launch` is pure -- no filesystem, no ``os.environ``, no clock -- so every rule
here is tested on Linux CI. The caller locates the executables and passes its environment.

**One field, two readers.** ``opmode`` decides who runs the app:

- ``operated`` -- the supervisor launches VSCode on ``<top>/mast-<role>.code-workspace``
  **once**, and never again; the operator presses F5. VSCode gets the environment unchanged.
- ``controlled`` -- the supervisor launches the app itself, from ``<top>/<role>/src`` (the
  import root ``from unit import Unit`` needs), and restarts it when it exits.

**``controlled`` is refused for spec.** MAST_spec does not read ``opmode`` yet, so a spec app
started by the supervisor would start its hardware at once while the supervisor reported it
waiting. The unit acts on ``opmode`` since MAST_unit#297: under ``controlled`` it waits for
``startup``.

**The app's environment** is the supervisor's, which is the interactive session's, with:

- ``http_proxy`` / ``https_proxy`` removed, whatever their case: the app talks to PWI4 on
  ``127.0.0.1``, and on Windows ``os.environ`` reports both upper-cased.
- ``PYTHONUNBUFFERED=1``, so the app's output reaches the supervisor's log as it is written.
- ``MAST_OPMODE`` only as inherited. Injecting the resolved database value would make it an
  environment override, which outranks the database in ``common.opmode.resolve_opmode``.

No ``PYTHONPATH``: ``mast.pth`` already puts ``<top>`` on the path.

Design: mast-claude-config ``plans/supervisor-design.md`` §8 and §15.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType

from supervision.state import OpMode

APP_ROLES = frozenset({"unit", "spec"})
CONTROLLED_ROLES = frozenset({"unit"})  # the roles whose app waits for `startup` under `controlled`

PROXY_VARIABLES = frozenset({"http_proxy", "https_proxy"})
UNBUFFERED = {"PYTHONUNBUFFERED": "1"}


class LaunchError(Exception):
    """The launch cannot be built: a role with no app, a refused opmode, or no VSCode to start."""


@dataclass(frozen=True, slots=True)
class LaunchSettings:
    """What :func:`build_launch` reads besides role and opmode, with the executables already located.

    ``workspace`` is ``common.config.supervisor.VSCodeConfig.workspace``; a local type, so nothing
    here imports ``common.config``.
    """

    python: Path  # the supervisor's own interpreter, which is the role's venv
    vscode_exe: Path | None  # None when VSCode was not found
    workspace: str  # a file name under <top>; {role} is the machine role


@dataclass(frozen=True, slots=True)
class LaunchSpec:
    """One process to start. ``supervised`` is false for a launch made once and never watched."""

    name: str
    argv: tuple[str, ...]
    cwd: Path
    env: Mapping[str, str] = field(repr=False)
    supervised: bool


def build_launch(role: str, opmode: OpMode, top: Path, conf: LaunchSettings, env: Mapping[str, str]) -> LaunchSpec:
    if role not in APP_ROLES:
        raise LaunchError(f"role {role!r} has no app to launch; expected one of {', '.join(sorted(APP_ROLES))}")
    match opmode:
        case OpMode.OPERATED:
            return _vscode(role, top, conf, env)
        case OpMode.CONTROLLED:
            return _app(role, top, conf, env)


def app_entry(role: str, top: Path) -> Path:
    """The role's ``app.py``. Its folder is the app's import root and working directory."""
    return top / role / "src" / "app.py"


def _vscode(role: str, top: Path, conf: LaunchSettings, env: Mapping[str, str]) -> LaunchSpec:
    if conf.vscode_exe is None:
        raise LaunchError("opmode operated launches VSCode, and VSCode was not found")
    workspace = top / conf.workspace.format(role=role)
    return LaunchSpec(
        name="vscode",
        argv=(str(conf.vscode_exe), str(workspace)),
        cwd=top,
        env=MappingProxyType(dict(env)),
        supervised=False,
    )


def _app(role: str, top: Path, conf: LaunchSettings, env: Mapping[str, str]) -> LaunchSpec:
    if role not in CONTROLLED_ROLES:
        raise LaunchError(
            f"opmode controlled is refused for {role}: its app does not read opmode yet and would start "
            "its hardware at once. Set opmode operated for this machine."
        )
    app = app_entry(role, top)
    app_env = {k: v for k, v in env.items() if k.lower() not in PROXY_VARIABLES} | UNBUFFERED
    return LaunchSpec(
        name="app",
        argv=(str(conf.python), str(app)),
        cwd=app.parent,
        env=MappingProxyType(app_env),
        supervised=True,
    )
