"""The programs the supervisor owns or watches, resolved from the configuration: what to run, how to probe.

``SupervisorConfig.processes`` names programs and says how to treat each; this module knows
what each name means on a MAST machine -- its image, where it is installed, the arguments it
needs and its health probe -- and turns one configuration entry into a :class:`ResolvedProgram`.
The supervisor loop builds its rows from these, and ``--dry-run`` prints them, so the two
cannot disagree about what would be started.

A program that cannot be started -- not installed, its catalog missing, or a name this module
does not know -- resolves with a ``problem`` and no spawn; an observed program needs only its
image. ps3cli's default arguments are MAST_unit's ``app.py`` launch command, copied
(MAST_supervision#2).

Design: mast-claude-config ``plans/supervisor-design.md`` §6 and §10.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from supervision.locate import PS3CLI_EXE, locate_ps3cli_catalog, locate_ps3cli_dir, locate_pwi4_exe
from supervision.managed import RestartPolicy
from supervision.probes import PS3CLI_PORT, HealthResult, probe_phd2, probe_ps3cli, probe_pwi4
from supervision.state import SupervisionMode

if TYPE_CHECKING:
    from common.config.supervisor import ManagedProcessConfig


@dataclass(frozen=True, slots=True)
class Located:
    exe: Path | None
    args: tuple[str, ...] = ()
    problem: str = ""  # why it cannot be started, when it cannot


@dataclass(frozen=True, slots=True)
class Known:
    image: str
    probe: Callable[[], HealthResult]
    locate: Callable[[Mapping[str, str], Path], Located]


@dataclass(frozen=True, slots=True)
class ResolvedProgram:
    name: str
    mode: SupervisionMode
    argv: tuple[str, ...]  # argv[0]'s file name is the image looked for
    probe: Callable[[], HealthResult]
    policy: RestartPolicy
    problem: str = ""  # set: nothing is started, and the row reports this

    def describe(self) -> str:
        if self.problem:
            return self.problem
        if self.mode is SupervisionMode.OBSERVED:
            return "observed, not started"
        return " ".join(self.argv)


def _pwi4(_env: Mapping[str, str], _home: Path) -> Located:
    exe = locate_pwi4_exe()
    return Located(exe) if exe else Located(None, problem="PWI4.exe not found")


def _ps3cli(env: Mapping[str, str], home: Path) -> Located:
    folder = locate_ps3cli_dir(env, home)
    if folder is None:
        return Located(None, problem=f"{PS3CLI_EXE} not found")
    catalog = locate_ps3cli_catalog(env, home)
    if catalog is None:
        return Located(None, problem="PlateSolve catalog (UC4 and Orca) not found; ps3cli --server would exit")
    return Located(folder / PS3CLI_EXE, ("--server", f"--port={PS3CLI_PORT}", f"--root-path={catalog}"))


def _never(_env: Mapping[str, str], _home: Path) -> Located:
    return Located(None, problem="the supervisor does not start this program")


KNOWN: dict[str, Known] = {
    "pwi4": Known("PWI4.exe", probe_pwi4, _pwi4),
    "phd2": Known("phd2.exe", probe_phd2, _never),  # observed until MAST_unit stops spawning it
    "ps3cli": Known(PS3CLI_EXE, probe_ps3cli, _ps3cli),
}


def restart_policy(conf: ManagedProcessConfig, stop_grace_seconds: float) -> RestartPolicy:
    return RestartPolicy(
        probe_interval_seconds=conf.probe_interval_seconds,
        unhealthy_threshold=conf.unhealthy_threshold,
        restart_backoff_seconds=conf.restart_backoff_seconds,
        restart_backoff_cap_seconds=conf.restart_backoff_cap_seconds,
        crash_loop_restarts=conf.crash_loop_restarts,
        crash_loop_window_seconds=conf.crash_loop_window_seconds,
        stop_grace_seconds=stop_grace_seconds,
    )


def resolve_programs(
    processes: Mapping[str, ManagedProcessConfig], stop_grace_seconds: float, env: Mapping[str, str], home: Path
) -> list[ResolvedProgram]:
    """Every program not disabled, in configuration order."""
    resolved = []
    for name, conf in processes.items():
        mode = SupervisionMode(conf.mode)
        if mode is SupervisionMode.DISABLED:
            continue
        policy = restart_policy(conf, stop_grace_seconds)
        known = KNOWN.get(name)
        if known is None:
            resolved.append(ResolvedProgram(name, mode, (name,), _unknown, policy, f"no program named {name!r}"))
            continue
        if mode is SupervisionMode.OBSERVED:
            resolved.append(ResolvedProgram(name, mode, (known.image,), known.probe, policy))
            continue
        located = Located(Path(conf.exe)) if conf.exe else known.locate(env, home)
        if located.exe is None:
            resolved.append(ResolvedProgram(name, mode, (known.image,), known.probe, policy, located.problem))
            continue
        args = tuple(conf.args) or located.args
        resolved.append(ResolvedProgram(name, mode, (str(located.exe), *args), known.probe, policy))
    return resolved


def _unknown() -> HealthResult:
    return HealthResult(False, "unknown program")
