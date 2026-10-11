"""``mast-supervisor``: ``python -m supervision``.

- ``--dry-run`` resolves the machine's role, ``opmode`` and configuration, locates the programs,
  prints what the supervisor would start, and exits non-zero when the launch cannot be built.
- ``--no-gui --no-service`` runs the supervisor loop in this console until Ctrl-C, logging to
  ``mast-<role>-supervisor-log.txt``. The window arrives in stage 3 part 3 and the service in
  stage 5; until then both flags are required. ``--no-app`` launches nothing and watches the app.
  ``GET /status`` is served on ``SUPERVISOR_PORT`` throughout; a supervisor that cannot have the
  port does not start (see :mod:`supervision.instance`).

A control machine is refused: it is Linux, and runs no unit or spec app.

``common.config`` is imported inside functions, never at module scope: importing it builds a
``Filer`` and imports pymongo.

Design: mast-claude-config ``plans/supervisor-design.md`` §8 and §15.
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import common
from supervision.launcher import APP_ROLES, LaunchError, LaunchSpec, build_launch
from supervision.state import OpMode

REPORTED_ENV = ("MAST_OPMODE", "PYTHONUNBUFFERED")


@dataclass(frozen=True, slots=True)
class Plan:
    role: str
    opmode: OpMode
    top: Path
    programs: dict[str, str]  # name -> what would be started, or why nothing is
    launch: LaunchSpec | None
    refused: str | None = None  # why there is no launch
    target_missing: bool = False  # the workspace or app.py the launch opens does not exist


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mast-supervisor")
    parser.add_argument("--dry-run", action="store_true", help="print what would be started, and exit")
    parser.add_argument("--no-gui", action="store_true", help="no window (required until stage 3 part 3)")
    parser.add_argument("--no-service", action="store_true", help="run in this console (required until stage 5)")
    parser.add_argument("--no-app", action="store_true", help="launch neither VSCode nor the app; watch the app")
    args = parser.parse_args(argv)
    if not args.dry_run and not (args.no_gui and args.no_service):
        parser.error("until the window and the service exist, run with --no-gui --no-service (or --dry-run)")

    from common.config.local import load_local_config

    role = load_local_config().machine_role
    if role not in APP_ROLES:
        print(f"mast-supervisor: refusing to run on a {role} machine", file=sys.stderr)
        return 1
    top = Path(common.__file__).resolve().parents[1]
    if args.dry_run:
        plan = resolve_plan(role, top, os.environ)
        print(render_plan(plan))
        return 0 if plan.launch and not plan.target_missing else 1
    return run(role, top, no_app=args.no_app)


def run(role: str, top: Path, no_app: bool) -> int:
    """The port is claimed before the loop starts, so a second supervisor never reaches the programs."""
    from common.const import Const
    from common.mast_logging import LogProgram, configure_logging, get_logger
    from supervision.api import StatusServer, build_app
    from supervision.instance import ClaimError, PsutilPortOwners, claim_port
    from supervision.supervisor import Supervisor
    from supervision.wiring import hostname, real_wiring

    configure_logging(program=LogProgram.SUPERVISOR)
    env = dict(os.environ)
    wiring = real_wiring(role, env, Path.home())
    supervisor = Supervisor(role, hostname(), top, env, wiring, no_app=no_app)
    try:
        sock = claim_port(Const.SUPERVISOR_PORT, PsutilPortOwners(), wiring.clock)
    except ClaimError as e:
        get_logger(__name__).error(f"mast-supervisor: not starting: {e}")
        return 1
    server = StatusServer(build_app(lambda: supervisor.snapshot), sock)
    server.start()
    try:
        supervisor.run(threading.Event())
    except KeyboardInterrupt:
        print("mast-supervisor: stopped; the programs it owns are left running", file=sys.stderr)
    finally:
        server.stop()
    return 0


def resolve_plan(role: str, top: Path, env: Mapping[str, str]) -> Plan:
    from supervision.wiring import load_config, real_wiring

    loaded = load_config(role)
    wiring = real_wiring(role, env, Path.home())
    programs = {program.name: program.describe() for program in wiring.resolve_programs(loaded)}
    try:
        launch = build_launch(role, loaded.opmode, top, wiring.launch_settings(loaded), env)
    except LaunchError as error:
        return Plan(role, loaded.opmode, top, programs, launch=None, refused=str(error))
    missing = not Path(launch.argv[-1]).exists()
    return Plan(role, loaded.opmode, top, programs, launch=launch, target_missing=missing)


def render_plan(plan: Plan) -> str:
    lines = [f"role: {plan.role}", f"opmode: {plan.opmode}", f"top: {plan.top}", "programs:"]
    lines += [f"  {name}: {where}" for name, where in plan.programs.items()] or ["  none"]
    spec = plan.launch
    if spec is None:
        lines.append(f"launch: REFUSED -- {plan.refused}")
        return "\n".join(lines)
    watched = "supervised" if spec.supervised else "launched once, not supervised"
    lines += [f"launch: {spec.name} ({watched})", f"  argv: {' '.join(spec.argv)}", f"  cwd: {spec.cwd}"]
    lines += [f"  env {name}: {value}" for name, value in spec.env.items() if name in REPORTED_ENV]
    if plan.target_missing:
        lines.append(f"  MISSING: {spec.argv[-1]}")
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())
