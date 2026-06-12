"""Master cycle deadline.

The cycle workflow has a hard job-level timeout (cycle.yml ~28 min).
We give the python work a softer deadline (cycle_deadline_minutes,
default 20) plumbed into every phase so each strategy can check it
between units of work and exit cleanly instead of getting SIGTERM'd by
GitHub Actions.

The rule: a cycle must NEVER die by timeout. It must always reach the
phase-timing table, the ledger.db guard step, and the commit step --
even when that means scanning less. Strategies degrade by truncating
their inner loops, log how many units they skipped, and return whatever
they completed.

Pass `Deadline.none()` to disable bounding (used by manual CLI commands
where the operator wants a full sweep).
"""
from __future__ import annotations

import time


class Deadline:
    """A wall-clock deadline expressed as a target Unix timestamp.

    Helpers:
      - left()       seconds until deadline (negative once expired)
      - expired()    True when no time remains
      - none()       sentinel never-expires deadline (factory)
    """

    __slots__ = ("_ts",)

    def __init__(self, target_ts: float):
        self._ts = float(target_ts)

    @classmethod
    def in_minutes(cls, minutes: float) -> "Deadline":
        return cls(time.time() + max(0.0, float(minutes)) * 60.0)

    @classmethod
    def none(cls) -> "Deadline":
        # Far-future sentinel; left() stays huge so checks always pass.
        return cls(time.time() + 365 * 24 * 3600.0)

    @classmethod
    def coerce(cls, x: "Deadline | None") -> "Deadline":
        """Accept None as 'no deadline'. Used at API boundaries that
        want to keep `deadline=None` as a default."""
        return x if isinstance(x, Deadline) else cls.none()

    def left(self) -> float:
        return self._ts - time.time()

    def expired(self) -> bool:
        return time.time() >= self._ts

    def elapsed_label(self, started_at: float) -> str:
        """Format an `elapsed` figure relative to a phase start time.
        Used in 'deadline reached after Xm of work' log lines."""
        secs = max(0.0, time.time() - started_at)
        m, s = divmod(secs, 60.0)
        if m >= 1:
            return f"{int(m)}m{int(s):02d}s"
        return f"{secs:.1f}s"

    def __repr__(self) -> str:
        return f"Deadline(left={self.left():.1f}s)"
