"""``mast-supervisor``: ``python -m supervision``.

- ``--dry-run`` resolves the machine's role, ``opmode`` and configuration, locates the programs,
  prints what the supervisor would start, and exits non-zero when the launch cannot be built.
- ``--service`` is how ``mast-service`` starts the supervisor in the console session. It runs
  until the service signals the stop event (:mod:`supervision.session`), then tears the programs
  down, the app first; its children break away from the service's job.
- ``--no-service`` runs the supervisor in this console until Ctrl-C, which leaves the programs
  running, as a developer's stop must.
- Either logs to ``mast-<role>-supervisor-log.txt`` and opens the window
  (:mod:`supervision.gui`) unless ``--no-gui`` is given or no window can open. ``--no-app``
  launches nothing and watches the app. Exactly one of ``--dry-run``, ``--service`` and
  ``--no-service`` is given; ``--dry-run`` takes no other option.
  ``GET /status`` is served on ``SUPERVISOR_PORT`` throughout; a supervisor that cannot have the
  port does not start (see :mod:`supervision.instance`).
- With the window, the Tk mainloop owns the main thread and the loop runs on a worker. Ctrl-C
  is a SIGINT handler that sets the stop event, since Tk would swallow a ``KeyboardInterrupt``
  raised inside its callbacks.

A control machine is refused: it is Linux, and runs no unit or spec app.

``common.config`` is imported inside functions, never at module scope: importing it builds a
``Filer`` and imports pymongo.

Design: mast-claude-config ``plans/supervisor-design.md`` §8 and §15.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

import common
from supervision import gui_model
from supervision.launcher import APP_ROLES, LaunchError, LaunchSpec, build_launch
from supervision.state import Launch, OpMode

if TYPE_CHECKING:
    from supervision.supervisor import Supervisor

REPORTED_ENV = ("MAST_OPMODE", "PYTHONUNBUFFERED")
RUN_OPTIONS = {"--service": "service", "--no-service": "no_service", "--no-gui": "no_gui", "--no-app": "no_app"}


@dataclass(frozen=True, slots=True)
class Plan:
    role: str
    opmode: OpMode
    top: Path
    programs: dict[str, str]  # name -> what would be started, or why nothing is
    launch: LaunchSpec | None
    refused: str | None = None  # why there is no launch
    target_missing: bool = False  # the workspace or app.py the launch opens does not exist


@dataclass(frozen=True, slots=True)
class Args:
    launch: Launch | None  # None: a dry run
    no_gui: bool
    no_app: bool


def parse_args(argv: Sequence[str] | None) -> Args:
    parser = argparse.ArgumentParser(prog="mast-supervisor")
    parser.add_argument("--dry-run", action="store_true", help="print what would be started, and exit")
    launch = parser.add_mutually_exclusive_group()
    launch.add_argument("--service", action="store_true", help="as started by mast-service; stopped by its stop event")
    launch.add_argument("--no-service", action="store_true", help="run in this console until Ctrl-C")
    parser.add_argument("--no-gui", action="store_true", help="no window: run headless")
    parser.add_argument("--no-app", action="store_true", help="launch neither VSCode nor the app; watch the app")
    args = parser.parse_args(argv)
    if args.dry_run:
        given = [flag for flag, on in RUN_OPTIONS.items() if getattr(args, on)]
        if given:
            parser.error(f"--dry-run starts nothing and takes no {', '.join(given)}")
        return Args(launch=None, no_gui=False, no_app=False)
    if not args.service and not args.no_service:
        parser.error("say how this supervisor runs: --service (mast-service), --no-service (a console) or --dry-run")
    return Args(Launch.SERVICE if args.service else Launch.CONSOLE, args.no_gui, args.no_app)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)

    from common.config.local import load_local_config

    role = load_local_config().machine_role
    if role not in APP_ROLES:
        print(f"mast-supervisor: refusing to run on a {role} machine", file=sys.stderr)
        return 1
    top = Path(common.__file__).resolve().parents[1]
    if args.launch is None:
        plan = resolve_plan(role, top, os.environ)
        print(render_plan(plan))
        return 0 if plan.launch and not plan.target_missing else 1
    return run(role, top, args.launch, no_app=args.no_app, gui=not args.no_gui)


class Window(Protocol):
    def run(self, stop: threading.Event, alive: Callable[[], bool]) -> None: ...


def run(role: str, top: Path, launch: Launch, no_app: bool, gui: bool) -> int:
    """Both ports are claimed before the loop starts, so a second supervisor never reaches the programs
    and none runs without its routes. Under the service the stop event is opened first: a
    ``--service`` supervisor nothing can stop does not start."""
    from common.mast_logging import LogProgram, configure_logging, get_logger
    from supervision.api import start_servers
    from supervision.instance import LISTENERS, ClaimError, PsutilPortOwners, claim_listeners
    from supervision.supervisor import StopFlavor, Supervisor, stop_flavor
    from supervision.wiring import hostname, real_wiring

    configure_logging(program=LogProgram.SUPERVISOR)
    logger = get_logger(__name__)
    stop = threading.Event()
    if launch is Launch.SERVICE:
        from supervision.session import PyWin32SessionApi, StopEventError, open_stop_event, watch_stop_event

        api = PyWin32SessionApi()
        try:
            event = open_stop_event(api)
        except StopEventError as e:
            logger.error(f"mast-supervisor: not starting: {e}")
            return 1
        threading.Thread(target=watch_stop_event, args=(api, event, stop), name="service-stop", daemon=True).start()
    env = dict(os.environ)
    wiring = real_wiring(role, env, Path.home(), launch)
    supervisor = Supervisor(role, hostname(), top, env, wiring, no_app=no_app)
    try:
        status_sock, action_sock = claim_listeners(LISTENERS, PsutilPortOwners(), wiring.clock)
    except ClaimError as e:
        logger.error(f"mast-supervisor: not starting: {e}")
        return 1
    servers = start_servers(status_sock, action_sock, lambda: supervisor.snapshot, wiring.ramdisk, supervisor)
    flavor = stop_flavor(launch)
    try:
        window = open_gui(supervisor, top, launch) if gui else None
        if window is None:
            supervisor.run(stop, flavor)
        else:
            run_with_window(lambda stopped: supervisor.run(stopped, flavor), window, stop)
    except KeyboardInterrupt:
        pass
    finally:
        for server in servers:
            server.stop()
    if flavor is StopFlavor.TEAR_DOWN and stop.is_set():
        print("mast-supervisor: stopped; the programs it owns were torn down", file=sys.stderr)
    else:
        print("mast-supervisor: stopped; the programs it owns are left running", file=sys.stderr)
    return 0


def open_gui(supervisor: Supervisor, top: Path, launch: Launch) -> Window | None:
    """The window, fed by a ``DequeHandler`` on the root logger; None to run headless."""
    from common.config.supervisor import GuiConfig
    from common.mast_logging import DailyFileHandler, get_logger
    from supervision.logsink import DequeHandler

    try:
        from supervision import gui
    except ImportError as e:  # a Python built without Tk
        get_logger(__name__).warning(f"no window ({e}); running headless")
        return None
    conf = GuiConfig()  # the window opens before the configuration can be read
    root = logging.getLogger()
    daily = next((h for h in root.handlers if isinstance(h, DailyFileHandler)), None)
    handler = DequeHandler(conf.log_buffer_records)
    if daily is not None:
        handler.setFormatter(daily.formatter)
    root.addHandler(handler)
    settings = gui.WindowSettings(
        conf.drain_interval_ms, conf.drain_max_records_per_tick, conf.log_pane_max_lines, conf.log_level
    )
    log_file = Path(daily.baseFilename) if daily is not None else None
    context = gui.WindowContext(
        gui_model.package_version(), top, log_file, Path(__file__).with_name("README.md"), gui_model.close_hint(launch)
    )
    window = gui.open_window(lambda: supervisor.snapshot, handler.lines, settings, context, supervisor.wall_clock)
    if window is None:
        root.removeHandler(handler)
    return window


def run_with_window(loop: Callable[[threading.Event], None], window: Window, stop: threading.Event) -> None:
    """The window on this thread, ``loop`` on a worker; Ctrl-C, a closed loop or ``stop`` ends both.

    An exception that ends the loop closes the window and is raised here, as it would be headless.
    """
    failures: list[BaseException] = []

    def work() -> None:
        try:
            loop(stop)
        except BaseException as e:  # noqa: BLE001 -- carried to the main thread and raised there
            failures.append(e)

    worker = threading.Thread(target=work, name="supervisor-loop")
    previous = signal.signal(signal.SIGINT, lambda signum, frame: stop.set())
    worker.start()
    try:
        window.run(stop, worker.is_alive)
    finally:
        stop.set()
        worker.join()
        signal.signal(signal.SIGINT, previous)
    if failures:
        raise failures[0]


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
