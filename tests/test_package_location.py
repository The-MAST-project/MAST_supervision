"""`supervision` must resolve to this checkout, not to an installed distribution.

PyPI carries an unrelated, popular ``supervision`` package (Roboflow's computer-vision
library). mast.pth appends <top> AFTER site-packages, so if that package is ever
installed into the fleet venv it silently shadows this one.
"""

from pathlib import Path

import supervision

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_supervision_imports_from_this_checkout() -> None:
    assert Path(supervision.__file__).resolve().parent == REPO_ROOT
