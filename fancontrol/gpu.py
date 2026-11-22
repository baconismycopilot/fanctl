"""GPU lighting as a target namespace.

Like memory, a graphics card's lighting has no liquidctl driver and no USB
device behind it, so there is nothing for targets.resolve() to discover. OpenRGB
reaches it over the card's own I2C bus, so this namespace is resolved through
the OpenRGB SDK alone, before device discovery.

The RTX 5090 Founders Edition here exposes three one-LED zones ("0 - SINGLE
COLOR" through "2 - SINGLE COLOR") and exactly two modes, Off and Direct. With
no Static mode to hold a colour, 'fixed' is sent as Direct, one colour per LED.

Zones are numbered by the card's own zone index, which the firmware fixes, so
'gpu:1' keeps meaning the same light across reboots. Plain 'gpu' is every zone.
"""

from __future__ import annotations

from dataclasses import dataclass

from .backends import openrgb_be
from .targets import TargetError

ALIASES = ("gpu", "graphics")


@dataclass
class GpuZone:
    """One lighting zone on a graphics card, under a stable name."""

    index: int  # 1-based, card order then zone order
    zone: openrgb_be.Zone

    @property
    def label(self) -> str:
        return f"gpu:{self.index}"

    def __str__(self) -> str:
        return self.label


def is_gpu(token: str) -> bool:
    """True if this token names GPU lighting, with or without a zone index."""
    head, _, _ = token.strip().lower().partition(":")
    return head in ALIASES


def _index(token: str) -> int | None:
    _, _, tail = token.strip().lower().partition(":")
    tail = tail.strip()
    if tail in ("", "all", "sync"):
        return None
    if not tail.isdigit() or int(tail) < 1:
        raise TargetError(
            f"{token!r}: a GPU zone index is a number from 1, e.g. 'gpu:1'; "
            "plain 'gpu' means every zone"
        )
    return int(tail)


def discover(client) -> list[GpuZone]:
    return [GpuZone(index=i, zone=z) for i, z in enumerate(openrgb_be.find_gpu(client), start=1)]


def select(zones: list[GpuZone], token: str) -> list[GpuZone]:
    """The zones a token names. Raises rather than silently doing nothing."""
    if not zones:
        raise TargetError(
            "OpenRGB reports no GPU lighting\n"
            "  it reaches the card over its I2C bus, which needs the i2c-dev module "
            "and a card OpenRGB supports"
        )

    wanted = _index(token)
    if wanted is None:
        return zones
    for zone in zones:
        if zone.index == wanted:
            return [zone]
    known = ", ".join(z.label for z in zones)
    raise TargetError(f"no {token!r}; found {len(zones)} zone(s): {known}")


def has_static(zone: GpuZone) -> bool:
    from .colors import MODES

    native = (MODES["fixed"].openrgb or "").strip().lower()
    return any(m.strip().lower() == native for m in zone.zone.modes)


def modes_for(zone: GpuZone) -> list[str]:
    """Canonical modes this zone supports, counting 'fixed' emulated by Direct."""
    from .caps import canonical_modes

    modes = set(canonical_modes(zone.zone.modes, "openrgb")) | {"off"}
    if "direct" in modes:
        modes.add("fixed")
    return sorted(modes)


def native_mode(zone: GpuZone, mode: str) -> str:
    """The canonical mode to actually send: 'fixed' becomes Direct without a Static."""
    if mode == "fixed" and not has_static(zone):
        return "direct"
    return mode
