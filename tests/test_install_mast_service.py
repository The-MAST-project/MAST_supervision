"""install/install-mast-service.ps1 against what MAST_provisioning's mast-service verify expects.

The installer is the only place `nssm install mast-service` happens: provisioning's
no-service scan stops at its own tree, so these text checks are the guard on the real
registration. The expected values are MAST_provisioning's
`server/providers/mast-service/verify-mast-service.ps1` (MAST_provisioning#262).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from supervision.service import SUPERVISOR_STOP_GRACE_SECONDS

INSTALLER = Path(__file__).resolve().parents[1] / "install" / "install-mast-service.ps1"

# MAST_provisioning's verify, its nssm provider and its service-name accessor.
SERVICE_NAME = "mast-service"
NSSM_EXE = r"C:\Program Files\nssm\nssm.exe"
VENV_PYTHON = r".venv\Scripts\python.exe"
SERVICE_MODULE = "supervision.service"
NSSM_PARAMETERS = (
    "Application",
    "AppParameters",
    "AppDirectory",
    "ObjectName",
    "Start",
    "DisplayName",
    "Description",
    "AppStdout",
    "AppStderr",
    "AppRotateFiles",
    "AppRotateOnline",
    "AppRotateBytes",
    "AppRestartDelay",
    "AppStopMethodSkip",
    "AppStopMethodConsole",
    "AppKillProcessTree",
    "AppNoConsole",
)
WM_CLOSE_AND_THREADS_SKIPPED = 2 | 4
MS_PER_SECOND = 1000


@pytest.fixture(scope="module")
def source() -> str:
    return INSTALLER.read_text(encoding="utf-8")


def _constant(source: str, name: str) -> str:
    match = re.search(rf"^\$\{{{name}\}}\s*=\s*(.+?)\s*$", source, re.MULTILINE)
    assert match, f"no ${{{name}}} constant in the installer"
    return match.group(1).strip("'")


def _number(source: str, name: str) -> int:
    return int(_constant(source, name))


def _code(source: str) -> str:
    return "\n".join(line.split("#", 1)[0] for line in source.splitlines())


def test_it_registers_the_name_provisioning_manages(source: str) -> None:
    assert _constant(source, "SERVICE_NAME") == SERVICE_NAME


def test_it_uses_the_nssm_provisioning_installs(source: str) -> None:
    assert _constant(source, "NSSM_EXE") == NSSM_EXE


def test_the_service_runs_the_service_module_with_the_venv_python(source: str) -> None:
    assert _constant(source, "VENV_PYTHON") == VENV_PYTHON
    assert _constant(source, "SERVICE_MODULE") == SERVICE_MODULE
    assert re.search(r"'-m\s*\{0\}'\s*-f\s*\$\{SERVICE_MODULE\}", source), "AppParameters must be '-m <module>'"


def test_top_is_two_levels_above_the_script(source: str) -> None:
    """<Top>\\supervision\\install\\install-mast-service.ps1, as provisioning's provider calls it."""
    assert re.search(r"Split-Path -Parent \(Split-Path -Parent \$PSScriptRoot\)", source)


def test_it_runs_as_localsystem_and_starts_automatically(source: str) -> None:
    assert _constant(source, "SERVICE_ACCOUNT") == "LocalSystem"
    assert _constant(source, "START_MODE") == "SERVICE_AUTO_START"


@pytest.mark.parametrize("parameter", NSSM_PARAMETERS)
def test_every_parameter_is_set_on_each_run(source: str, parameter: str) -> None:
    """Set after install, every run, so a re-run converges a changed or stale registration."""
    assert re.search(rf"'{parameter}'", _code(source)), f"nssm set {parameter} missing"


def test_an_existing_service_is_not_installed_again(source: str) -> None:
    code = _code(source)
    assert code.count("'install'") == 1
    install = code.index("'install'")
    guard = code.rfind("if ($null -eq ${existing}) {", 0, install)
    assert guard != -1, "nssm install only inside the service-absent branch"
    assert "\n}" not in code[guard:install], "the service-absent branch closes before nssm install"


def test_the_installer_never_starts_the_service(source: str) -> None:
    """Provisioning's finalize starts it; standdown stops it around every run."""
    code = _code(source)
    assert "Start-Service" not in code
    assert "'start'" not in code
    assert "sc.exe start" not in code


def test_restart_delay_and_failure_actions(source: str) -> None:
    assert _number(source, "APP_RESTART_DELAY_MS") == 5000
    assert re.search(r"-Path 'sc\.exe' -Arguments @\('failure', \$\{SERVICE_NAME\}, 'reset='", _code(source))
    assert _number(source, "FAILURE_RESTARTS") == 3


def test_only_ctrl_c_and_terminate_are_used_to_stop(source: str) -> None:
    assert _number(source, "STOP_METHOD_SKIP") == WM_CLOSE_AND_THREADS_SKIPPED


def test_a_stop_does_not_kill_the_process_tree(source: str) -> None:
    """The supervised programs are descendants and must survive; the job takes the supervisor."""
    assert _number(source, "KILL_PROCESS_TREE") == 0


def test_the_stop_grace_is_service_pys(source: str) -> None:
    """service.py waits this long after signaling the stop before closing the job."""
    assert _number(source, "SUPERVISOR_STOP_GRACE_SECONDS") == SUPERVISOR_STOP_GRACE_SECONDS


def test_the_console_timeout_is_the_grace_plus_a_margin(source: str) -> None:
    code = _code(source)
    assert re.search(
        r"\$\{STOP_CONSOLE_TIMEOUT_MS\}\s*=\s*\(\$\{SUPERVISOR_STOP_GRACE_SECONDS\}\s*\+\s*"
        r"\$\{STOP_MARGIN_SECONDS\}\)\s*\*\s*\$\{MS_PER_SECOND\}",
        code,
    )
    assert _number(source, "STOP_MARGIN_SECONDS") > 0
    assert _number(source, "MS_PER_SECOND") == MS_PER_SECOND


def test_the_service_keeps_a_console(source: str) -> None:
    """NSSM's stop is a Ctrl+C, which needs a console to arrive on."""
    assert _number(source, "NO_CONSOLE") == 0


def test_uninstall_stops_then_removes(source: str) -> None:
    code = _code(source)
    assert re.search(r"'remove',\s*\$\{SERVICE_NAME\},\s*'confirm'", code)
    assert code.index("Stop-Service") < code.index("'remove'")


def test_logs_rotate_under_the_service_log_folder(source: str) -> None:
    assert _constant(source, "LOG_DIR") == r"C:\MAST\logs\mast-service"
    assert _number(source, "ROTATE_BYTES") > 0


def test_it_refuses_a_service_of_that_name_that_nssm_does_not_host(source: str) -> None:
    assert re.search(r"nssm\\\.exe", source)
