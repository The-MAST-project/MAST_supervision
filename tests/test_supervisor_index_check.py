import json
from pathlib import Path

import pytest

import supervision.resources as resources
from supervision.resources import NOT_PROVISIONED, IndexFile, index_set_mismatches, load_index_manifest, probe_ramdisk
from supervision.state import ResourceState

DECLARED = [IndexFile(name="index-5202-00.fits", size=3), IndexFile(name="index-5203-47.fits", size=5)]


def _write_manifest(path: Path, files: list[IndexFile], bom: bool = False) -> Path:
    text = json.dumps({"source": "mast-indexes", "files": [f.model_dump() for f in files]})
    path.write_bytes(("\ufeff" if bom else "").encode() + text.encode())
    return path


def _fill(index_dir: Path, files: list[IndexFile] = DECLARED) -> None:
    index_dir.mkdir(exist_ok=True)
    for f in files:
        (index_dir / f.name).write_bytes(b"x" * f.size)


@pytest.fixture
def mapped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(resources, "is_windows_drive_mapped", lambda drive: True)


@pytest.fixture
def manifest(tmp_path: Path) -> Path:
    return _write_manifest(tmp_path / "mast-indexes.json", DECLARED)


# --- the manifest ----------------------------------------------------------------------------------


def test_an_absent_manifest_is_none(tmp_path: Path) -> None:
    assert load_index_manifest(tmp_path / "absent.json") is None


@pytest.mark.parametrize("bom", [False, True])
def test_the_manifest_reads_with_or_without_a_bom(tmp_path: Path, bom: bool) -> None:
    assert load_index_manifest(_write_manifest(tmp_path / "m.json", DECLARED, bom=bom)) == DECLARED


def test_a_full_set_has_no_mismatches(tmp_path: Path) -> None:
    _fill(tmp_path / "idx")
    assert index_set_mismatches(DECLARED, tmp_path / "idx") == []


def test_missing_extra_and_wrong_sized_files_are_each_named(tmp_path: Path) -> None:
    index_dir = tmp_path / "idx"
    _fill(index_dir, [IndexFile(name="index-5202-00.fits", size=4), IndexFile(name="index-5206-00.fits", size=1)])

    assert index_set_mismatches(DECLARED, index_dir) == [
        "wrong size index-5202-00.fits: 4 bytes, expected 3",
        "missing index-5203-47.fits",
        "extra index-5206-00.fits",
    ]


@pytest.mark.parametrize("upper", ["declared", "on_disk"])
def test_names_compare_case_insensitively_as_windows_does(tmp_path: Path, upper: str) -> None:
    shouted = [IndexFile(name=f.name.upper(), size=f.size) for f in DECLARED]
    _fill(tmp_path / "idx", shouted if upper == "on_disk" else DECLARED)
    assert index_set_mismatches(shouted if upper == "declared" else DECLARED, tmp_path / "idx") == []


# --- the probe -------------------------------------------------------------------------------------


@pytest.mark.usefixtures("mapped")
def test_a_disk_holding_the_declared_set_is_ok(tmp_path: Path, manifest: Path) -> None:
    _fill(tmp_path / "idx")
    result = probe_ramdisk(str(tmp_path / "idx"), manifest)
    assert result.state is ResourceState.OK
    assert result.detail == "2 indexes"


@pytest.mark.usefixtures("mapped")
def test_a_set_that_differs_degrades_and_says_how(tmp_path: Path, manifest: Path) -> None:
    _fill(tmp_path / "idx", DECLARED[:1])
    result = probe_ramdisk(str(tmp_path / "idx"), manifest)
    assert result.state is ResourceState.DEGRADED
    assert result.detail == "1 of 2 index files differ: missing index-5203-47.fits"


@pytest.mark.usefixtures("mapped")
def test_without_a_manifest_the_set_is_not_checked(tmp_path: Path) -> None:
    _fill(tmp_path / "idx", [])
    result = probe_ramdisk(str(tmp_path / "idx"), tmp_path / "absent.json")
    assert result.state is ResourceState.OK
    assert result.detail == NOT_PROVISIONED


@pytest.mark.usefixtures("mapped")
def test_an_unreadable_manifest_degrades(tmp_path: Path) -> None:
    _fill(tmp_path / "idx")
    broken = tmp_path / "broken.json"
    broken.write_text("{not json")
    result = probe_ramdisk(str(tmp_path / "idx"), broken)
    assert result.state is ResourceState.DEGRADED
    assert result.detail.startswith(f"index manifest {broken} unreadable")


@pytest.mark.usefixtures("mapped")
@pytest.mark.parametrize("with_manifest", [True, False])
def test_a_missing_index_folder_is_down_with_or_without_a_manifest(
    tmp_path: Path, manifest: Path, with_manifest: bool
) -> None:
    path = manifest if with_manifest else tmp_path / "absent.json"
    assert probe_ramdisk(str(tmp_path / "absent"), path).state is ResourceState.DOWN


def test_an_unmapped_drive_is_down(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    asked = []
    monkeypatch.setattr(resources, "is_windows_drive_mapped", lambda drive: asked.append(drive) or False)
    assert probe_ramdisk("D:/mast-indexes", tmp_path / "absent.json").state is ResourceState.DOWN
    assert asked == ["D:"]
