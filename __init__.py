"""Process supervision for MAST unit and spec machines.

Two programs, one package:

- ``mast-service`` (``python -m supervision.service``): the session-0 Windows service,
  run by NSSM as LocalSystem. It starts and watches the supervisor in the interactive
  session and does nothing else.
- ``mast-supervisor`` (``python -m supervision``): the interactive supervisor. It waits
  for the machine's preconditions, owns PWI4 / PHD2 / ps3cli, launches VSCode or the
  unit/spec app according to ``opmode``, and reports through a window, a heartbeat in
  the log, and ``GET /mast/api/v1/supervisor/status``.

Design: ``mast-claude-config/plans/supervisor-design.md``.

This file imports nothing: ``win32*`` and ``tkinter`` are confined to the modules that
need them, so the package imports on Linux CI.
"""
