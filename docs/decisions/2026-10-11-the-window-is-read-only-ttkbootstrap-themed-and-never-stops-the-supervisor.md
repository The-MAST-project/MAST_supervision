---
decided: 2026-10-11
status: accepted
areas:
  - status-reporting
  - dependency management
---

# The window is read-only, themed with ttkbootstrap, and never stops the supervisor

**Why:** stage 3 part 3 adds the window (`plans/supervisor-design.md` §7). The design's window
carries the Actions menu, per-row restart buttons and an `opstate` label. The Actions menu waits
on a maintenance write path that MAST_common does not have yet (MAST_common#149), and on an
answer to how restart-to-apply restarts a supervisor before the service exists. So the window
was split from the actions to let the mast00 night run with a window sooner. Team decision,
2026-10-11.

**What:**

- **Read-only.** `gui.py` shows the header (*In maintenance* and *Opmode* large, then hostname,
  role, phase, uptime and config generation, with the configuration's degraded reason
  verbatim), the resource and process tables, and the rolling log with a level filter and a
  tail-follow toggle. The menu bar is About (version, `<top>`, host, role, phase, opmode, config
  generation, start time, log file, and a copy button) and Help (the README and the log folder).
  No Actions menu, no restart buttons, and no `opstate`: it lives only in the app's memory, so
  showing it needs the app to expose it first. All three are deferred, team decision 2026-10-11.
- **ttkbootstrap 2.2.3, exactly pinned**, with its `bootstrap-dark` theme for a window in a dome
  at night. Every widget is plain `ttk`, which the theme styles anyway; ttkbootstrap supplies
  only the themed root (`App`), its colors and `ScrolledText`. It is imported once, when `gui.py`
  is (`_ttkbootstrap_loads`), since it imports Pillow, whose native parts can fail to load; any
  `ImportError` makes the window plain Tk with named colors. Team decision, 2026-10-11.
- **Sizes follow the font.** ttkbootstrap's high-DPI scaling enlarges the fonts, so row heights
  come from the font's line height (`ROW_SPACING`), column widths are counted in characters
  (`COLUMN_CHARS`), and the log pane's natural height is 12 lines (`LOG_PANE_LINES`). Fixed pixel
  sizes clipped every row and truncated `supervised` and `not_supervised` on labcomp2's display.
  Each table's height follows its rows, so a program added to the configuration is never below
  the fold.
- **What the window says is decided in `gui_model.py`** (`header`, `resource_rows`,
  `process_rows`, `about_text`), from a snapshot and a wall clock, and tested without Tk.
  `gui.py` only places text. The uptime follows the wall clock between publications; the
  tables are redrawn only when a new snapshot is published.
- **Threads.** With the window, the Tk mainloop owns the main thread and `Supervisor.run` runs on
  a `supervisor-loop` worker (`__main__.run_with_window`). Ctrl-C is a SIGINT handler that sets
  the stop event, since Tk swallows a `KeyboardInterrupt` raised in its callbacks; the 100 ms
  drain tick is what lets Python run that handler. The window closes when the stop event is set
  or the loop ends; an exception that ended the loop is raised on the main thread after the
  window closes, as it would be headless.
- **The way out cannot fail.** Tk drops an `after` callback that raises, so the stop/alive check
  is a tick of its own (`_watch_tick`) that does nothing else, and the drain and refresh ticks
  re-arm in a `finally`. A configured `gui.log_level` that is not a filter level starts the filter
  at INFO with a WARNING, rather than failing on the first line drained.
- **The window never stops the supervisor.** Closing it minimizes it and logs how to stop the
  supervisor. A `TclError` on opening, any other failure building it (logged with its
  traceback), or a Python with no Tk runs the supervisor headless, and the log handler that fed
  the window is removed. `--no-gui` asks for headless; `--no-service` is still required until stage 5.
- **The window opens before the configuration loads**, so its drain limits and level are
  `GuiConfig`'s defaults, as the resource wait uses `ResourcesConfig`'s.

**Checked on Windows** (labcomp2's console session, 2026-10-11, Tk 8.6, Pillow 12.3.0), with
the real window, worker thread and SIGINT handler, fed a changing snapshot: themed and plain
both opened, showed the snapshot and the severities, kept 300 filtered DEBUG lines out of the
pane, minimized on the title bar's close (`iconic`) while the loop kept ticking, and stopped in
about 5 s with no loop thread left. Screen grabs are refused in that session, so the window was
captured with `PrintWindow`. A configured level of `verbose` started the filter at INFO with its
warning. The suite passed there (255).

**Rejected:**

- *ttkbootstrap's `bootstyle` widgets with a shim that drops the keyword without it.* The shim's
  fallback import needed a type-check suppression, and plain `ttk` widgets take the theme anyway.
- *Ship the window with the Actions menu.* It would wait on MAST_common#149.
- *Tk calls from a helper thread in the check script.* Tk is not thread-safe; the check
  schedules its steps with `after()` on the Tk thread instead.

**Unsettled:**

- Not yet run on a unit with the real loop and configuration; that is the mast00 night.
- The menu bar is a classic Tk `Menu`, which Windows draws in its own light colors under the
  dark theme.

**Implications:** part 4 adds the Actions menu, the restart buttons and §13's Apply item, and
settles restart-to-apply before the service. `opstate` joins the header once the app exposes it.
