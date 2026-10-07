"""Test bootstrap for the MAST_supervision suite.

``common`` is a sibling clone (``<top>/common/``), reached through ``PYTHONPATH=<top>``
here and through ``mast.pth`` on a machine; nothing below places it.

``common.filer.Filer.__init__`` is shimmed to a temp-dir layout on **every** platform,
because ``common.utils`` builds a module-level ``Filer`` at import and ``common.config``
imports ``common.utils``, so the schema this package reads drags one in:

- On **macOS** the shim is what makes the import possible at all: ``Filer`` supports
  Windows/Linux only and raises at import time.
- On **Windows** the import succeeds, but an unshimmed ``Filer`` reaches for the
  machine's real storage roots, which a test suite has no business touching.

Also blocks external process launches -- see ``_block_external_processes`` below.
Copied from MAST_unit's ``tests/conftest.py``; consolidating the copies is
MAST_supervision#2.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from types import ModuleType
from typing import Any

# Programs a test may legitimately start. Keep this list short and justified: it is the
# one hole in the guard below.
#
# fontconfig -- matplotlib's font manager shells out to `fc-list` while importing, on
# Linux only. Kept in step with MAST_common and MAST_unit so the guards cannot drift.
ALLOWED = {"fc-list", "fc-match"}


class ProcessLaunchError(RuntimeError):
    """Raised when a test tries to start an external process."""


Doors = dict[ModuleType, tuple[str, ...]]


def _optional_doors() -> Doors:
    """Spawn entry points in modules that may not be installed: psutil, and win32 off Windows."""
    doors: Doors = {}
    try:
        import psutil

        doors[psutil] = ("Popen",)
    except ImportError:
        pass
    try:
        import win32process

        doors[win32process] = ("CreateProcess", "CreateProcessAsUser")
    except ImportError:
        pass
    return doors


def _executable_of(target: object) -> str:
    """Best-effort program name, whether the caller passed a list or a string."""
    if isinstance(target, (list, tuple)) and target:
        target = target[0]
    return os.path.basename(str(target)).lower()


def _check_launch(name: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
    target = args[0] if args else kwargs.get("args") or kwargs.get("cmd") or "?"
    if _executable_of(target) in ALLOWED:
        return
    raise ProcessLaunchError(
        f"the test suite tried to start a process via {name}: {target!r}. "
        "On a MAST machine this would drive real hardware. If a test genuinely "
        "needs this, allow it explicitly in tests/conftest.py."
    )


def _deny(name: str, original: Any) -> Any:
    """A stand-in for `original` that raises instead of launching.

    A class is replaced by a subclass whose constructor raises, not by a function:
    stdlib code subclasses these at import (on Windows, `asyncio.windows_utils` defines
    `class Popen(subprocess.Popen)`, reached through pymongo), and subclassing a function
    raises TypeError.
    """
    if isinstance(original, type):

        def _init(self: Any, *args: Any, **kwargs: Any) -> None:
            _check_launch(name, args, kwargs)
            original.__init__(self, *args, **kwargs)

        return type(original.__name__, (original,), {"__init__": _init})

    def denied(*args: Any, **kwargs: Any) -> Any:
        _check_launch(name, args, kwargs)
        return original(*args, **kwargs)

    return denied


def _block_external_processes() -> None:
    """Fail loudly if anything under test starts an external process.

    Installed at conftest IMPORT time, not from a fixture: collection imports every test
    module before any fixture runs, and a module importing an entry point at top level
    would spawn in that window.

    This package exists to start PWI4, PHD2, ps3cli and the unit app, and the suite runs
    on the machines where those drive real hardware. So the low-level entry points are
    blocked wholesale, and -- beyond MAST_unit's set -- ``win32process.CreateProcess`` and
    ``CreateProcessAsUser``, the session bridge's door into the interactive session. If a
    test genuinely needs a subprocess, allow it explicitly here rather than removing the
    guard.
    """
    denied: Doors = {
        subprocess: ("Popen", "run", "call", "check_call", "check_output"),
        os: ("system", "popen", "startfile", "execv", "execvp", "spawnl", "spawnv"),
        **_optional_doors(),
    }
    for module, names in denied.items():
        for name in names:
            original = getattr(module, name, None)
            if original is None:  # os.startfile is Windows-only, etc.
                continue
            setattr(module, name, _deny(f"{module.__name__}.{name}", original))


def _shim_filer_for_tests() -> None:
    import common.filer as filer_module

    tmp_root = tempfile.mkdtemp(prefix="mast-supervision-tests-")
    location = filer_module.Location(None, tmp_root)

    def _tmp_init(self: Any, logger: Any = None) -> None:
        self.local = location
        self.shared = location
        self.ram = location
        # `init_log` builds a `Filer()` and asks it for `machine_log_root()`, which reads
        # both. Every attribute the real `__init__` binds has to be here.
        self.share_root = location
        self.machine = location
        self.tops = {
            filer_module.FilerTop.Local: self.local,
            filer_module.FilerTop.Shared: self.shared,
            filer_module.FilerTop.Ram: self.ram,
        }
        self.logger = logger

    filer_module.Filer.__init__ = _tmp_init


_shim_filer_for_tests()
_block_external_processes()
