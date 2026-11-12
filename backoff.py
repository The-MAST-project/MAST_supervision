"""Doubling waits up to a cap.

Its own module, importing nothing, so ``service.py`` in session 0 can use it without
``managed.py``, whose logging import builds a ``Filer``.
"""

from __future__ import annotations


class Backoff:
    """Doubling waits up to a cap. A copy of MAST_common's ``config._watcher._Backoff`` (MAST_supervision#2)."""

    def __init__(self, first: float, cap: float) -> None:
        self._first = first
        self._cap = cap
        self._next = first

    def reset(self) -> None:
        self._next = self._first

    def next(self) -> float:
        value = self._next
        self._next = min(self._next * 2, self._cap)
        return value
