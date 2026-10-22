"""The real outside world the supervisor loop is given: the configuration database, the probes, the OS.

Everything here is what the tests replace. ``common.config`` is imported inside the functions,
never at module scope: importing it builds a ``Filer`` and imports pymongo.

Design: mast-claude-config ``plans/supervisor-design.md`` §5 and §8.
"""

from __future__ import annotations

import socket
import sys
from collections.abc import Mapping
from pathlib import Path, PureWindowsPath

from supervision.clock import Clock
from supervision.launcher import LaunchSettings
from supervision.locate import locate_vscode_exe
from supervision.managed import OsProcessApi
from supervision.probes import probe_app
from supervision.programs import ResolvedProgram, resolve_programs
from supervision.ramdisk import RamdiskReviver, probe_writable, run_command
from supervision.resources import ProbeResult, Resource, probe_network, probe_ramdisk, probe_share
from supervision.state import OpMode
from supervision.supervisor import ConfigStatus, LoadedConfig, ResourceSettings, Wiring


class ConfigMissingError(Exception):
    """The configuration database answered, but holds nothing for this machine."""


def hostname() -> str:
    return socket.gethostname().split(".")[0].lower()


def config_status(role: str) -> ConfigStatus:
    """Read from the in-memory snapshot the watcher keeps current; never reaches the database."""
    from common.config import Config

    config = Config()
    in_maintenance: bool | None = None
    if role == "unit":
        site = next((s for s in config.get_sites() if s.name == config.local.site), None)
        in_maintenance = site is not None and hostname() in site.units_in_maintenance
    return ConfigStatus(generation=config.generation, degraded_reason=config.degraded_reason, in_maintenance=in_maintenance)


def load_config(role: str, watch: bool = False) -> LoadedConfig:
    """``watch`` starts the configuration watcher: for the long-running loop, not a one-shot ``--dry-run``."""
    from common.config import Config
    from common.opmode import resolve_opmode

    config = Config()
    if watch:
        config.start_watching()
    match role:
        case "unit":
            unit = config.get_unit()
            if unit is None:
                raise ConfigMissingError(f"no configuration for unit {hostname()!r}")
            supervisor = unit.supervisor
        case _:
            supervisor = config.get_specs().supervisor
    service = config.get_service(role)
    if service is None:
        raise ConfigMissingError(f"no service named {role!r}, so the app's port is unknown")
    return LoadedConfig(
        opmode=OpMode(resolve_opmode()),
        supervisor=supervisor,
        app_port=service.port,
        status=config_status(role),
    )


def real_wiring(role: str, env: Mapping[str, str], home: Path) -> Wiring:
    from common.config.local import load_local_config
    from common.config.supervisor import ResourcesConfig, SupervisorConfig

    local = load_local_config()
    defaults = ResourcesConfig()  # the configuration cannot be read until the network is up

    def programs(loaded: LoadedConfig) -> list[ResolvedProgram]:
        conf = loaded.supervisor
        return resolve_programs(conf.processes, conf.stop_grace_seconds, env, home)

    def ramdisk_ready() -> ProbeResult:
        return probe_ramdisk(defaults.index_dir)

    clock = Clock()
    # Only a unit's D: is the ImDisk RAM disk. mast-ns-spec's D: is a fixed disk with no ImDisk task,
    # so it is neither probed as the RAM disk nor revived.
    ramdisk_root = PureWindowsPath(defaults.index_dir).anchor
    reviver = (
        RamdiskReviver(lambda: probe_writable(ramdisk_root), ramdisk_ready, run_command, clock) if role == "unit" else None
    )

    def launch_settings(loaded: LoadedConfig) -> LaunchSettings:
        vscode = loaded.supervisor.vscode
        exe = Path(vscode.exe) if vscode.exe else locate_vscode_exe(env)
        return LaunchSettings(python=Path(sys.executable), vscode_exe=exe, workspace=vscode.workspace)

    resource_probes = {
        Resource.NETWORK: lambda: probe_network(f"{local.controller_host}.{local.domain}", local.mongo_port),
        Resource.RAMDISK: ramdisk_ready,
        Resource.SHARE: probe_share,
    }
    if role != "unit":
        del resource_probes[Resource.RAMDISK]

    return Wiring(
        clock=clock,
        api=OsProcessApi(),
        resource_probes=resource_probes,
        resources=ResourceSettings(defaults.wait_budget_seconds, defaults.probe_interval_seconds),
        load_config=lambda: load_config(role, watch=True),
        config_status=lambda: config_status(role),
        resolve_programs=programs,
        launch_settings=launch_settings,
        app_probe=lambda port: probe_app(port, role),
        heartbeat_seconds=SupervisorConfig().heartbeat_interval_seconds,
        ramdisk=reviver,
    )
