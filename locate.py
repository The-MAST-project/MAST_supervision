"""Where the programs the supervisor starts are installed: VSCode, PWI4 and ps3cli.

Used when the configuration names no ``exe``. Interim, and copies (MAST_supervision#2 row 5):
the VSCode and PWI4 candidates are MAST_provisioning's ``provide-desktop-shortcuts.ps1``
lists, and the ps3cli search is MAST_unit's ``PlaneWave/ps3cli_locate.py``. Where programs are
installed is provisioning state, so the long-term source is a location registry provisioning
leaves on each machine. PHD2 is not located here: the supervisor only observes it until
MAST_unit stops spawning it.

The environment and home folder are parameters, so the searches are tested anywhere.

Design: mast-claude-config ``plans/supervisor-design.md`` §2 and §8.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path

VSCODE_RELATIVE = Path("Programs/Microsoft VS Code/Code.exe")  # under LOCALAPPDATA
VSCODE_FIXED = (
    Path(r"C:\Users\mast\AppData\Local\Programs\Microsoft VS Code\Code.exe"),
    Path(r"C:\Program Files\Microsoft VS Code\Code.exe"),
)
PWI4_CANDIDATES = (
    Path(r"C:\Program Files (x86)\PlaneWave Instruments\PlaneWave Interface 4\PWI4.exe"),
    Path(r"C:\Program Files\PlaneWave Instruments\PlaneWave Interface 4\PWI4.exe"),
)

PS3CLI_EXE = "ps3cli.exe"
PS3CLI_FIXED_ROOT = Path(r"C:\Program Files (x86)\PlaneWave Instruments\ps3cli")
PS3CLI_HOME_ROOT = Path("Documents/PlaneWave/ps3cli")
CATALOG_HOME_ROOTS = (Path("Documents/Kepler"), Path("Downloads/PlaneWave/Platesolve3.80/Kepler"))
CATALOG_SUBDIRS = ("UC4", "Orca")


def locate_vscode_exe(env: Mapping[str, str]) -> Path | None:
    return first_file(vscode_candidates(env))


def locate_pwi4_exe() -> Path | None:
    return first_file(PWI4_CANDIDATES)


def locate_ps3cli_dir(env: Mapping[str, str], home: Path) -> Path | None:
    """The folder holding ``ps3cli.exe``; ``PS3CLI_DIR``, set machine-wide by provisioning, first."""
    return ps3cli_dir_under(_env_path(env, "PS3CLI_DIR") + [home / PS3CLI_HOME_ROOT, PS3CLI_FIXED_ROOT])


def locate_ps3cli_catalog(env: Mapping[str, str], home: Path) -> Path | None:
    """The PlateSolve3 star catalog ``ps3cli --server`` needs, not the astrometry.net indexes."""
    return catalog_under(_env_path(env, "PS3CLI_CATALOG") + [home / root for root in CATALOG_HOME_ROOTS])


def vscode_candidates(env: Mapping[str, str]) -> list[Path]:
    local = [Path(env["LOCALAPPDATA"]) / VSCODE_RELATIVE] if env.get("LOCALAPPDATA") else []
    return local + list(VSCODE_FIXED)


def first_file(candidates: Iterable[Path]) -> Path | None:
    return next((path for path in candidates if path.is_file()), None)


def ps3cli_dir_under(roots: Iterable[Path]) -> Path | None:
    """The first root holding a ``ps3cli.exe`` at any depth, and in it the largest one's folder.

    The ``--server`` build sits in a dated folder whose name changes per build, and an older
    on-demand build (~10 KB) can sit beside it; the server build (~4 MB) is the largest.
    """
    for root in roots:
        if not root.is_dir():
            continue
        exes = [path for path in root.rglob(PS3CLI_EXE) if path.is_file()]
        if exes:
            return max(exes, key=lambda path: path.stat().st_size).parent
    return None


def catalog_under(roots: Iterable[Path]) -> Path | None:
    """The first root that is a catalog, or has one as a direct child."""
    for root in roots:
        if not root.is_dir():
            continue
        for candidate in [root, *sorted(path for path in root.iterdir() if path.is_dir())]:
            if all((candidate / sub).is_dir() for sub in CATALOG_SUBDIRS):
                return candidate
    return None


def _env_path(env: Mapping[str, str], name: str) -> list[Path]:
    return [Path(env[name])] if env.get(name) else []
