# MAST_supervision — agent notes

- **Design source:** `mast-claude-config/plans/supervisor-design.md`. Read it before changing behavior; its decisions are ratified with Arie.
- **Integration branch:** `main`.
- **Layout:** the repo root is the `supervision` package, imported through `mast.pth` from `<top>/supervision/`. Never pip-install it; never set `PYTHONPATH` on a machine (tests set it to `<top>`).
- **Import boundaries:** `win32*` only in `session.py` / `service.py`; `tkinter` (and `ttkbootstrap`, if adopted) only in `gui.py`. Nothing may reach for `Config()`, `Filer()`, `win32*` or `tkinter` at import time — the suite runs on Linux CI.
- **What belongs in MAST_common instead:** anything another program imports — the `SupervisorConfig` schema, port constants, `opmode`, process helpers.
- **Dependencies:** `pyproject.toml`. Lower bounds for libraries shared with another MAST repo, exact pins for ones only this repo uses; see `DECISIONS.md`.
- **Checks before done:** `uv run ruff format --check .`, `uv run ruff check .`, `PYTHONPATH=.. uv run pytest`.
