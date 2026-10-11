"""``mast-supervisor``: ``python -m supervision``.

Only ``--dry-run`` until stage 3 adds the supervisor loop: it resolves the machine's role,
``opmode`` and configuration, locates the programs, prints what the supervisor would start,
and exits non-zero when the launch cannot be built.

``common.config`` is imported inside :func:`resolve_plan`, never at module scope: importing it
builds a ``Filer`` and imports pymongo.

Design: mast-claude-config ``plans/supervisor-design.md`` §8 and §15.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import common
from supervision.launcher import LaunchError, LaunchSettings, LaunchSpec, build_launch
from supervision.locate import PS3CLI_EXE, locate_ps3cli_catalog, locate_ps3cli_dir, locate_pwi4_exe, locate_vscode_exe
from supervision.state import OpMode

if TYPE_CHECKING:
    from common.config.supervisor import ManagedProcessConfig

NOT_FOUND = "not found"
REPORTED_ENV = ("MAST_OPMODE", "PYTHONUNBUFFERED")


@dataclass(frozen=True, slots=True)
class Plan:
    role: str
    opmode: OpMode
    top: Path
    programs: dict[str, str]  # name -> its executable, or why none is started
    launch: LaunchSpec | None
    refused: str | None = None  # why there is no launch
    target_missing: bool = False  # the workspace or app.py the launch opens does not exist


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mast-supervisor")
    parser.add_argument("--dry-run", action="store_true", help="print what would be started, and exit")
    args = parser.parse_args(argv)
    if not args.dry_run:
        parser.error("only --dry-run is available until stage 3 adds the supervisor loop")
    plan = resolve_plan(os.environ)
    print(render_plan(plan))
    return 0 if plan.launch and not plan.target_missing else 1


def resolve_plan(env: Mapping[str, str]) -> Plan:
    from common.config import Config
    from common.config.local import load_local_config
    from common.config.supervisor import SupervisorConfig
    from common.opmode import resolve_opmode

    role = load_local_config().machine_role
    opmode = OpMode(resolve_opmode())
    match role:
        case "unit":
            unit = Config().get_unit()
            if unit is None:
                raise SystemExit("this unit has no configuration in the config database")
            conf = unit.supervisor
        case "spec":
            conf = Config().get_specs().supervisor
        case _:
            conf = SupervisorConfig()

    top = Path(common.__file__).resolve().parents[1]
    home = Path.home()
    ps3cli_dir = locate_ps3cli_dir(env, home)
    located = {
        "pwi4": locate_pwi4_exe(),
        "ps3cli": ps3cli_dir / PS3CLI_EXE if ps3cli_dir else None,
    }
    programs = describe_programs(conf.processes, located, locate_ps3cli_catalog(env, home))

    vscode_exe = Path(conf.vscode.exe) if conf.vscode.exe else locate_vscode_exe(env)
    settings = LaunchSettings(python=Path(sys.executable), vscode_exe=vscode_exe, workspace=conf.vscode.workspace)
    try:
        launch = build_launch(role, opmode, top, settings, env)
    except LaunchError as error:
        return Plan(role=role, opmode=opmode, top=top, programs=programs, launch=None, refused=str(error))
    missing = not Path(launch.argv[-1]).exists()
    return Plan(role=role, opmode=opmode, top=top, programs=programs, launch=launch, target_missing=missing)


def describe_programs(
    processes: Mapping[str, ManagedProcessConfig], located: Mapping[str, Path | None], catalog: Path | None
) -> dict[str, str]:
    """Each program the supervisor would own or watch, with its executable or why none is started."""
    from common.config.supervisor import SupervisionMode

    programs: dict[str, str] = {}
    for name, process in processes.items():
        match process.mode:
            case SupervisionMode.DISABLED:
                continue
            case SupervisionMode.OBSERVED:
                programs[name] = "observed, not started"
            case SupervisionMode.SUPERVISED:
                programs[name] = process.exe or str(located.get(name) or NOT_FOUND)
    if "ps3cli" in programs:
        programs["ps3cli catalog"] = str(catalog or NOT_FOUND)
    return programs


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
