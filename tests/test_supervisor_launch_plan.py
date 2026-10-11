from pathlib import Path
from types import MappingProxyType

import pytest

from supervision.__main__ import Plan, main, render_plan
from supervision.launcher import LaunchError, LaunchSettings, build_launch
from supervision.state import OpMode

TOP = Path("/top")
PYTHON = Path("/top/.venv/bin/python")
VSCODE = Path("/vscode/Code.exe")
WORKSPACE = "mast-{role}.code-workspace"
SETTINGS = LaunchSettings(python=PYTHON, vscode_exe=VSCODE, workspace=WORKSPACE)
ENV = {"PATH": "/bin", "PS3CLI_DIR": "/ps3cli"}


@pytest.mark.parametrize("role", ["unit", "spec"])
def test_development_launches_vscode_once_on_the_role_workspace(role: str) -> None:
    spec = build_launch(role, OpMode.DEVELOPMENT, TOP, SETTINGS, ENV)

    assert spec.name == "vscode"
    assert spec.argv == (str(VSCODE), str(TOP / f"mast-{role}.code-workspace"))
    assert spec.cwd == TOP
    assert spec.supervised is False


def test_development_hands_vscode_the_environment_unchanged() -> None:
    env = ENV | {"http_proxy": "http://proxy:8080"}

    assert dict(build_launch("unit", OpMode.DEVELOPMENT, TOP, SETTINGS, env).env) == env


def test_the_workspace_name_comes_from_the_settings() -> None:
    settings = LaunchSettings(python=PYTHON, vscode_exe=VSCODE, workspace="dev-{role}.code-workspace")

    assert build_launch("unit", OpMode.DEVELOPMENT, TOP, settings, ENV).argv[1] == str(TOP / "dev-unit.code-workspace")


def test_development_without_vscode_is_an_error() -> None:
    settings = LaunchSettings(python=PYTHON, vscode_exe=None, workspace=WORKSPACE)
    with pytest.raises(LaunchError, match="VSCode was not found"):
        build_launch("unit", OpMode.DEVELOPMENT, TOP, settings, ENV)


def test_production_unit_runs_the_app_from_src_with_the_venv_python() -> None:
    spec = build_launch("unit", OpMode.PRODUCTION, TOP, SETTINGS, ENV)

    assert spec.name == "app"
    assert spec.argv == (str(PYTHON), str(TOP / "unit" / "src" / "app.py"))
    assert spec.cwd == TOP / "unit" / "src"
    assert spec.supervised is True


def test_production_does_not_need_vscode() -> None:
    settings = LaunchSettings(python=PYTHON, vscode_exe=None, workspace=WORKSPACE)

    assert build_launch("unit", OpMode.PRODUCTION, TOP, settings, ENV).name == "app"


def test_production_is_refused_for_spec() -> None:
    with pytest.raises(LaunchError, match="refused for spec"):
        build_launch("spec", OpMode.PRODUCTION, TOP, SETTINGS, ENV)


@pytest.mark.parametrize("opmode", list(OpMode))
def test_a_role_with_no_app_is_an_error(opmode: OpMode) -> None:
    with pytest.raises(LaunchError, match="no app"):
        build_launch("control", opmode, TOP, SETTINGS, ENV)


@pytest.mark.parametrize("name", ["http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "Http_Proxy"])
def test_the_app_gets_no_proxy_whatever_the_case(name: str) -> None:
    env = build_launch("unit", OpMode.PRODUCTION, TOP, SETTINGS, ENV | {name: "http://proxy:8080"}).env

    assert name not in env
    assert env["PATH"] == ENV["PATH"]


def test_the_app_runs_unbuffered() -> None:
    assert build_launch("unit", OpMode.PRODUCTION, TOP, SETTINGS, ENV).env["PYTHONUNBUFFERED"] == "1"


def test_the_app_gets_no_pythonpath_of_its_own() -> None:
    assert "PYTHONPATH" not in build_launch("unit", OpMode.PRODUCTION, TOP, SETTINGS, ENV).env


def test_mast_opmode_is_never_injected() -> None:
    assert "MAST_OPMODE" not in build_launch("unit", OpMode.PRODUCTION, TOP, SETTINGS, ENV).env


def test_an_inherited_mast_opmode_is_passed_through() -> None:
    env = build_launch("unit", OpMode.PRODUCTION, TOP, SETTINGS, ENV | {"MAST_OPMODE": "production"}).env

    assert env["MAST_OPMODE"] == "production"


def test_the_callers_environment_is_left_alone() -> None:
    env = ENV | {"http_proxy": "http://proxy:8080"}

    spec = build_launch("unit", OpMode.PRODUCTION, TOP, SETTINGS, env)

    assert env == ENV | {"http_proxy": "http://proxy:8080"}
    assert isinstance(spec.env, MappingProxyType)


def test_the_plan_shows_the_launch() -> None:
    spec = build_launch("unit", OpMode.PRODUCTION, TOP, SETTINGS, ENV)

    text = render_plan(Plan("unit", OpMode.PRODUCTION, TOP, {"phd2": "observed, not started"}, spec))

    assert "launch: app (supervised)" in text
    assert "  phd2: observed, not started" in text
    assert "  env PYTHONUNBUFFERED: 1" in text


def test_the_plan_shows_a_refusal() -> None:
    text = render_plan(Plan("spec", OpMode.PRODUCTION, TOP, {}, None, refused="no"))

    assert "launch: REFUSED -- no" in text
    assert "  none" in text


def test_the_plan_marks_a_missing_target() -> None:
    spec = build_launch("unit", OpMode.DEVELOPMENT, TOP, SETTINGS, ENV)

    text = render_plan(Plan("unit", OpMode.DEVELOPMENT, TOP, {}, spec, target_missing=True))

    assert f"  MISSING: {TOP / 'mast-unit.code-workspace'}" in text


@pytest.mark.parametrize("argv", [[], ["--no-gui"], ["--no-app"], ["--no-gui", "--no-app"]])
def test_until_the_service_exists_no_service_is_required(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        main(argv)

    assert raised.value.code == 2
