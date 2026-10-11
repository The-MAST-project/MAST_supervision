from pathlib import Path

import pytest

from supervision.locate import (
    VSCODE_FIXED,
    VSCODE_RELATIVE,
    catalog_under,
    first_file,
    locate_ps3cli_catalog,
    locate_ps3cli_dir,
    ps3cli_dir_under,
    vscode_candidates,
)

SERVER_BUILD_BYTES = 4096
ON_DEMAND_BUILD_BYTES = 10


def touch(path: Path, size: int = 1) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path


def make_catalog(path: Path) -> Path:
    for sub in ("UC4", "Orca"):
        (path / sub).mkdir(parents=True)
    return path


def test_vscode_user_install_comes_first() -> None:
    assert vscode_candidates({"LOCALAPPDATA": "/local"}) == [Path("/local") / VSCODE_RELATIVE, *VSCODE_FIXED]


def test_vscode_without_localappdata_tries_the_fixed_paths() -> None:
    assert vscode_candidates({}) == list(VSCODE_FIXED)


def test_first_file_skips_missing_paths_and_folders(tmp_path: Path) -> None:
    (tmp_path / "folder").mkdir()
    exe = touch(tmp_path / "Code.exe")

    assert first_file([tmp_path / "missing.exe", tmp_path / "folder", exe]) == exe


def test_first_file_finds_nothing(tmp_path: Path) -> None:
    assert first_file([tmp_path / "missing.exe"]) is None


@pytest.mark.parametrize(
    ("server_folder", "on_demand_folder"), [("ps3cli-2024", "ps3cli-2023"), ("ps3cli-2023", "ps3cli-2024")]
)
def test_ps3cli_is_found_in_a_dated_folder_and_the_server_build_wins(
    tmp_path: Path, server_folder: str, on_demand_folder: str
) -> None:
    touch(tmp_path / on_demand_folder / "ps3cli.exe", ON_DEMAND_BUILD_BYTES)
    server = touch(tmp_path / server_folder / "ps3cli.exe", SERVER_BUILD_BYTES)

    assert ps3cli_dir_under([tmp_path]) == server.parent


def test_ps3cli_takes_the_first_root_that_has_one(tmp_path: Path) -> None:
    first = touch(tmp_path / "a" / "ps3cli.exe", ON_DEMAND_BUILD_BYTES)
    touch(tmp_path / "b" / "ps3cli.exe", SERVER_BUILD_BYTES)

    assert ps3cli_dir_under([tmp_path / "missing", tmp_path / "a", tmp_path / "b"]) == first.parent


def test_ps3cli_dir_env_wins(tmp_path: Path) -> None:
    override = touch(tmp_path / "override" / "ps3cli.exe")
    touch(tmp_path / "home" / "Documents" / "PlaneWave" / "ps3cli" / "ps3cli.exe")

    assert locate_ps3cli_dir({"PS3CLI_DIR": str(tmp_path / "override")}, tmp_path / "home") == override.parent


def test_ps3cli_falls_back_to_the_home_documents(tmp_path: Path) -> None:
    exe = touch(tmp_path / "Documents" / "PlaneWave" / "ps3cli" / "build" / "ps3cli.exe")

    assert locate_ps3cli_dir({}, tmp_path) == exe.parent


def test_a_catalog_is_the_root_or_a_direct_child(tmp_path: Path) -> None:
    root_catalog = make_catalog(tmp_path / "root")
    child_catalog = make_catalog(tmp_path / "parent" / "Kepler")

    assert catalog_under([root_catalog]) == root_catalog
    assert catalog_under([tmp_path / "parent"]) == child_catalog


def test_a_folder_with_half_a_catalog_is_not_one(tmp_path: Path) -> None:
    (tmp_path / "UC4").mkdir()

    assert catalog_under([tmp_path]) is None


def test_ps3cli_catalog_env_wins(tmp_path: Path) -> None:
    override = make_catalog(tmp_path / "override")
    make_catalog(tmp_path / "home" / "Documents" / "Kepler")

    assert locate_ps3cli_catalog({"PS3CLI_CATALOG": str(override)}, tmp_path / "home") == override


def test_ps3cli_catalog_falls_back_to_the_home_folders(tmp_path: Path) -> None:
    catalog = make_catalog(tmp_path / "Downloads" / "PlaneWave" / "Platesolve3.80" / "Kepler")

    assert locate_ps3cli_catalog({}, tmp_path) == catalog
