from pathlib import Path

import pytest

import supervision.resources as resources
from supervision.resources import index_file_names, missing_indexes, probe_ramdisk
from supervision.state import ResourceState

SERIES = 5206
COUNT = 47


def _fill(index_dir: Path) -> None:
    for name in index_file_names(SERIES, COUNT):
        (index_dir / name).write_bytes(b"x")


@pytest.fixture
def mapped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(resources, "is_windows_drive_mapped", lambda drive: True)


def test_the_set_is_series_5206_00_to_46() -> None:
    names = index_file_names(SERIES, COUNT)
    assert (names[0], names[-1], len(names)) == ("index-5206-00.fits", "index-5206-46.fits", 47)


def test_a_full_set_misses_nothing(tmp_path: Path) -> None:
    _fill(tmp_path)
    assert missing_indexes(tmp_path, SERIES, COUNT) == []


def test_a_missing_file_is_missing(tmp_path: Path) -> None:
    _fill(tmp_path)
    (tmp_path / "index-5206-17.fits").unlink()
    assert missing_indexes(tmp_path, SERIES, COUNT) == ["index-5206-17.fits"]


def test_a_zero_length_file_is_missing(tmp_path: Path) -> None:
    # What a half-finished copy leaves behind; exists() alone is satisfied by it.
    _fill(tmp_path)
    (tmp_path / "index-5206-03.fits").write_bytes(b"")
    assert missing_indexes(tmp_path, SERIES, COUNT) == ["index-5206-03.fits"]


@pytest.mark.usefixtures("mapped")
def test_a_full_ramdisk_is_ok(tmp_path: Path) -> None:
    _fill(tmp_path)
    assert probe_ramdisk(str(tmp_path), SERIES, COUNT).state is ResourceState.OK


@pytest.mark.usefixtures("mapped")
def test_missing_indexes_degrade_rather_than_fail(tmp_path: Path) -> None:
    _fill(tmp_path)
    (tmp_path / "index-5206-00.fits").unlink()
    result = probe_ramdisk(str(tmp_path), SERIES, COUNT)
    assert result.state is ResourceState.DEGRADED
    assert result.detail == "1 of 47 indexes missing or empty"


@pytest.mark.usefixtures("mapped")
def test_a_missing_index_folder_is_down(tmp_path: Path) -> None:
    assert probe_ramdisk(str(tmp_path / "absent"), SERIES, COUNT).state is ResourceState.DOWN


def test_an_unmapped_drive_is_down(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    asked = []
    monkeypatch.setattr(resources, "is_windows_drive_mapped", lambda drive: asked.append(drive) or False)
    assert probe_ramdisk("D:/mast-indexes", SERIES, COUNT).state is ResourceState.DOWN
    assert asked == ["D:"]
