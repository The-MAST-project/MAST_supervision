import subprocess
import sys

import psutil
import pytest
from conftest import ProcessLaunchError


def test_subprocess_cannot_start_a_process() -> None:
    with pytest.raises(ProcessLaunchError):
        subprocess.run(["pwi4.exe"])


def test_psutil_cannot_start_a_process() -> None:
    with pytest.raises(ProcessLaunchError):
        psutil.Popen(["pwi4.exe"])


def test_a_blocked_class_can_still_be_subclassed() -> None:
    # asyncio.windows_utils does exactly this at import, on Windows.
    class Child(subprocess.Popen):
        pass

    assert issubclass(Child, subprocess.Popen)
    with pytest.raises(ProcessLaunchError):
        Child(["pwi4.exe"])


@pytest.mark.skipif(sys.platform != "win32", reason="win32process exists only on Windows")
def test_win32process_cannot_start_a_process() -> None:
    import win32process

    for create in (win32process.CreateProcess, win32process.CreateProcessAsUser):
        with pytest.raises(ProcessLaunchError):
            create(None, "pwi4.exe", None, None, False, 0, None, None, None)
