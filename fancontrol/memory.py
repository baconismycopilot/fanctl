"""RGB memory as a target namespace.

DIMM lighting sits outside every assumption the rest of fanctl makes about a
device. There is no liquidctl driver, so nothing to discover or derive
capabilities from; there is no USB serial, so nothing to key an alias on; and
there is no hidraw node, so the flock in lock.py protects nothing here. It is an
ENE controller answering on the SMBus at the DIMMs' SPD addresses, reachable
through OpenRGB and nowhere else.

So memory gets its own small namespace rather than being forced through
targets.resolve(). Sticks are numbered in SMBus address order -- a property of
which slot the stick is in, not of OpenRGB's detection order -- so 'memory:1'
keeps meaning the same stick across reboots.
"""

from __future__ import annotations

from dataclasses import dataclass

from .backends import openrgb_be
from .targets import TargetError

# What a user might reasonably type for the same thing.
ALIASES = ("memory", "mem", "ram", "dram")


@dataclass
class Stick:
    """One DIMM's lighting zone, under a stable name."""

    index: int  # 1-based, in SMBus address order
    zone: openrgb_be.Zone

    @property
    def label(self) -> str:
        return f"memory:{self.index}"

    @property
    def address(self) -> str:
        return self.zone.address or "?"

    def __str__(self) -> str:
        return self.label


def is_memory(token: str) -> bool:
    """True if this token names memory, with or without a stick index."""
    head, _, _ = token.strip().lower().partition(":")
    return head in ALIASES


def _index(token: str) -> int | None:
    """The stick a token asks for, or None for all of them."""
    _, _, tail = token.strip().lower().partition(":")
    tail = tail.strip()
    if tail in ("", "all", "sync"):
        return None
    if not tail.isdigit() or int(tail) < 1:
        raise TargetError(
            f"{token!r}: a memory index is a stick number from 1, e.g. 'memory:1'; "
            "plain 'memory' means every stick"
        )
    return int(tail)


def discover(client) -> list[Stick]:
    return [Stick(index=i, zone=z) for i, z in enumerate(openrgb_be.find_memory(client), start=1)]


def select(sticks: list[Stick], token: str) -> list[Stick]:
    """The sticks a token names. Raises rather than silently doing nothing."""
    if not sticks:
        raise TargetError(
            "OpenRGB reports no RGB memory\n"
            "  it reads DIMM lighting over the SMBus, which needs the i2c-dev module "
            "and an OpenRGB build with I2C detection enabled"
        )

    wanted = _index(token)
    if wanted is None:
        return sticks
    for stick in sticks:
        if stick.index == wanted:
            return [stick]
    known = ", ".join(s.label for s in sticks)
    raise TargetError(f"no {token!r}; found {len(sticks)} stick(s): {known}")


def modes_for(stick: Stick) -> list[str]:
    """Canonical mode names this stick's controller actually implements."""
    from .caps import canonical_modes

    return sorted(set(canonical_modes(stick.zone.modes, "openrgb")) | {"off"})
