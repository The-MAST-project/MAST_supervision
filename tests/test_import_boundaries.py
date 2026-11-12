"""What the package may import, checked on the source rather than trusted to review."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# session.py and backoff.py are what service.py (session 0) will import. The conftest
# imports common.filer for every test, so this is read from the source, not sys.modules.
SESSION_ZERO_IMPORTS = {
    "session.py": {"__future__", "dataclasses", "logging", "threading", "typing", "supervision.backoff"},
    "backoff.py": {"__future__"},
}


def _package_sources() -> list[Path]:
    return [p for p in REPO_ROOT.rglob("*.py") if ".venv" not in p.parts and "tests" not in p.parts]


def _module_level_imports(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def _all_imports(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


@pytest.mark.parametrize("path", _package_sources(), ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_no_ctypes(path: Path) -> None:
    """Win32 goes through pywin32: the test spawn guard patches ``win32process``, and a
    ``ctypes`` call to ``CreateProcessAsUser`` would walk around it."""
    assert "ctypes" not in _all_imports(path)


@pytest.mark.parametrize("name", sorted(SESSION_ZERO_IMPORTS))
def test_session_zero_modules_import_nothing_that_reaches_the_filer(name: str) -> None:
    """``service.py`` runs in session 0, where the share and the RAM disk mean nothing, and
    ``common.mast_logging`` builds a ``Filer`` at import."""
    assert _module_level_imports(REPO_ROOT / name) <= SESSION_ZERO_IMPORTS[name]
