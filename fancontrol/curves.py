"""Curve interpolation and the governor that decides when to write.

The governor exists because writing a duty on every poll is both wasteful and
audible. Fans hunting up and down a few percent is more annoying than a fan
that simply runs slightly fast, so changes are damped two ways:

- A duty must move at least `min_step` before it is written at all.
- Spinning *down* requires twice that. Temperature falls the moment load drops,
  but it rises again just as fast during bursty work; making the downward move
  reluctant stops the fan sawing during compiles.
"""

from __future__ import annotations

from dataclasses import dataclass


def interpolate(curve: list[tuple[int, int]], temp: float) -> int:
    """Linear interpolation between curve points, clamped outside the range."""
    if not curve:
        raise ValueError("empty curve")

    if temp <= curve[0][0]:
        return curve[0][1]
    if temp >= curve[-1][0]:
        return curve[-1][1]

    for (t0, d0), (t1, d1) in zip(curve, curve[1:]):
        if t0 <= temp <= t1:
            if t1 == t0:
                return d1
            ratio = (temp - t0) / (t1 - t0)
            return round(d0 + ratio * (d1 - d0))

    return curve[-1][1]


@dataclass
class Governor:
    """Decides the duty for one channel, and whether it is worth writing."""

    curve: list[tuple[int, int]]
    floor: int
    min_step: int = 3
    applied: int | None = None

    def target(self, temp: float) -> int:
        return max(self.floor, interpolate(self.curve, temp))

    def update(self, temp: float, force: bool = False) -> int | None:
        """Return a duty to write, or None to leave the channel alone."""
        duty = self.target(temp)

        if force or self.applied is None:
            self.applied = duty
            return duty

        delta = duty - self.applied
        threshold = self.min_step if delta > 0 else self.min_step * 2
        if abs(delta) < threshold:
            return None

        self.applied = duty
        return duty
