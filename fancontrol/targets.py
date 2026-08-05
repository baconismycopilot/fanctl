"""Resolving user-facing channel names to a (device, channel) pair.

Accepted forms, in order of preference:

    hub:fan2      explicit device and channel
    hub:2         explicit device, numeric shorthand
    ring          bare channel, when exactly one device offers it
    all / sync    every channel on a device (requires an explicit device)

A bare name is only accepted when it is unambiguous across discovered devices.
Guessing between two devices that both have a 'fan2' would be a good way to spin
down the wrong loop.
"""

from __future__ import annotations

from dataclasses import dataclass

from .caps import Capabilities, inspect_device
from .devices import Device


class TargetError(ValueError):
    pass


@dataclass
class Target:
    device: Device
    channel: str
    caps: Capabilities

    def __str__(self) -> str:
        return f"{self.device.alias}:{self.channel}"


def _normalise(token: str, kind: str) -> str:
    """Expand numeric and 'all' shorthands into real channel names."""
    token = token.strip().lower()
    if token in ("all", "sync"):
        return "sync"
    if token.isdigit():
        return f"{kind}{token}"
    return token


def _channels_for(caps: Capabilities, kind: str) -> list[str]:
    if kind == "fan":
        channels = list(caps.speed_channels)
        if caps.speed_sync:
            channels.append("sync")
        return channels
    # color_targets already carries 'sync' when the driver defines it, plus
    # 'ring' when the ring is only reachable through OpenRGB.
    return caps.color_targets


def resolve(devices: list[Device], token: str, kind: str) -> Target:
    """Resolve a target token. `kind` is 'fan' or 'led'."""
    if ":" in token:
        alias, _, raw = token.partition(":")
        alias = alias.strip().lower()
        matches = [d for d in devices if d.alias == alias]
        if not matches:
            known = ", ".join(d.alias for d in devices) or "none"
            raise TargetError(f"unknown device {alias!r} (known: {known})")
        device = matches[0]
        caps = inspect_device(device)
        channel = _normalise(raw, kind)
        if channel not in _channels_for(caps, kind):
            raise TargetError(
                f"{device.alias} has no {kind} channel {channel!r}; "
                f"available: {', '.join(_channels_for(caps, kind)) or 'none'}"
            )
        return Target(device=device, channel=channel, caps=caps)

    channel = _normalise(token, kind)
    found: list[Target] = []
    for device in devices:
        caps = inspect_device(device)
        if channel in _channels_for(caps, kind):
            found.append(Target(device=device, channel=channel, caps=caps))

    if not found:
        raise TargetError(
            f"no device has a {kind} channel {channel!r}; "
            "use 'fanctl caps' to list what is available"
        )
    if len(found) > 1:
        options = ", ".join(str(t) for t in found)
        raise TargetError(f"{channel!r} is ambiguous; qualify it as one of: {options}")
    return found[0]


def sole_device_with(devices: list[Device], predicate, description: str) -> Target:
    """Find the one device satisfying `predicate`, for implicit targets.

    Used by commands like `pump` and `lcd`, where naming a device would be noise
    on a machine with a single cooler but must still be unambiguous.
    """
    found = []
    for device in devices:
        caps = inspect_device(device)
        channel = predicate(caps)
        if channel:
            found.append(Target(device=device, channel=channel, caps=caps))

    if not found:
        raise TargetError(f"no device supports {description}")
    if len(found) > 1:
        options = ", ".join(t.device.alias for t in found)
        raise TargetError(f"more than one device supports {description}: {options}")
    return found[0]
