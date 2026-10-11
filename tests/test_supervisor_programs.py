from pathlib import Path

import pytest

from common.config.supervisor import ManagedProcessConfig, SupervisorConfig
from common.config.supervisor import SupervisionMode as CommonMode
from supervision.programs import resolve_programs
from supervision.state import SupervisionMode

PWI4 = Path("/vendor/PWI4.exe")
PS3CLI_DIR = Path("/vendor/ps3cli-2024")
CATALOG = Path("/catalog")
GRACE = 10.0


@pytest.fixture(autouse=True)
def installed(monkeypatch) -> None:
    monkeypatch.setattr("supervision.programs.locate_pwi4_exe", lambda: PWI4)
    monkeypatch.setattr("supervision.programs.locate_ps3cli_dir", lambda env, home: PS3CLI_DIR)
    monkeypatch.setattr("supervision.programs.locate_ps3cli_catalog", lambda env, home: CATALOG)


def resolve(**processes: ManagedProcessConfig):
    return {p.name: p for p in resolve_programs(processes, GRACE, {}, Path("/home"))}


def test_the_unit_default_resolves_pwi4_and_ps3cli_and_observes_phd2() -> None:
    programs = {p.name: p for p in resolve_programs(SupervisorConfig.for_unit().processes, GRACE, {}, Path("/home"))}

    assert programs["pwi4"].argv == (str(PWI4),)
    assert programs["ps3cli"].argv == (
        str(PS3CLI_DIR / "ps3cli.exe"),
        "--server",
        "--port=8998",
        f"--root-path={CATALOG}",
    )
    assert programs["phd2"].mode is SupervisionMode.OBSERVED
    assert programs["phd2"].argv == ("phd2.exe",)
    assert not any(p.problem for p in programs.values())


def test_a_disabled_program_is_left_out() -> None:
    assert resolve(pwi4=ManagedProcessConfig(mode=CommonMode.DISABLED)) == {}


def test_an_observed_program_needs_only_its_image(monkeypatch) -> None:
    monkeypatch.setattr("supervision.programs.locate_pwi4_exe", lambda: None)

    program = resolve(pwi4=ManagedProcessConfig(mode=CommonMode.OBSERVED))["pwi4"]

    assert (program.argv, program.problem) == (("PWI4.exe",), "")
    assert program.describe() == "observed, not started"


def test_a_configured_exe_and_args_win_over_the_located_ones() -> None:
    program = resolve(ps3cli=ManagedProcessConfig(exe="D:/ps3cli.exe", args=["--server"]))["ps3cli"]

    assert program.argv == (str(Path("D:/ps3cli.exe")), "--server")


def test_a_program_not_found_is_a_problem_naming_its_image(monkeypatch) -> None:
    monkeypatch.setattr("supervision.programs.locate_pwi4_exe", lambda: None)

    program = resolve(pwi4=ManagedProcessConfig())["pwi4"]

    assert program.problem == "PWI4.exe not found"
    assert program.argv == ("PWI4.exe",)
    assert program.describe() == "PWI4.exe not found"


def test_ps3cli_without_its_catalog_is_a_problem(monkeypatch) -> None:
    monkeypatch.setattr("supervision.programs.locate_ps3cli_catalog", lambda env, home: None)

    assert "catalog" in resolve(ps3cli=ManagedProcessConfig())["ps3cli"].problem


def test_an_unknown_program_is_a_problem() -> None:
    assert resolve(ascom=ManagedProcessConfig())["ascom"].problem == "no program named 'ascom'"


def test_a_supervised_phd2_is_a_problem_until_the_supervisor_starts_it() -> None:
    assert resolve(phd2=ManagedProcessConfig())["phd2"].problem


def test_the_restart_policy_comes_from_the_configuration() -> None:
    conf = ManagedProcessConfig(probe_interval_seconds=7.0, unhealthy_threshold=2, crash_loop_restarts=9)

    policy = resolve(pwi4=conf)["pwi4"].policy

    assert (policy.probe_interval_seconds, policy.unhealthy_threshold, policy.crash_loop_restarts) == (7.0, 2, 9)
    assert policy.stop_grace_seconds == GRACE


def test_a_supervised_program_describes_its_command_line() -> None:
    assert resolve(pwi4=ManagedProcessConfig())["pwi4"].describe() == str(PWI4)
