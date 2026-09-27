"""The supervisor's two clocks, injected so every phase is testable without sleeping.

Budgets and intervals run on ``monotonic``; the timestamps a snapshot reports run on
``wall``. They are kept apart because the supervisor starts at boot, which is exactly when
NTP steps the wall clock: a wait budget measured on it could expire at once or never.

Design: mast-claude-config ``plans/supervisor-design.md`` §11.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class Clock:
    monotonic: Callable[[], float] = time.monotonic
    wall: Callable[[], datetime] = utc_now
