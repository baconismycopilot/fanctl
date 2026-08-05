"""Capability introspection.

This module exists because the capability boundaries on these devices are
invisible from the CLI and genuinely counterintuitive -- the Kraken 2024 Elite
*RGB* cannot set a colour through liquidctl at all, and the Control Hub silently
lacks device-side fan curves. Rather than encode that as folklore in a README,
we derive it from the installed driver classes and print it on demand.

Derivation is by design rather than by lookup table so that a liquidctl upgrade
that adds support shows up immediately instead of being masked by a stale table.

This reaches into liquidctl private attributes (`_speed_channels`,
`_color_channels`) and compares bound methods against known base implementations.
That API carries no stability guarantee, which is why the dependency is pinned
and every access here is defensive.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Backend(str, Enum):
    LIQUIDCTL = "liquidctl"
    OPENRGB = "openrgb"
    NONE = "none"


@dataclass
class Capabilities:
    """What a device can actually do, and which backend gets it done."""

    speed_channels: list[str] = field(default_factory=list)
    color_channels: list[str] = field(default_factory=list)
    speed_profile: bool = False  # device-side curve, no daemon required
    profile_temp_source: str | None = None
    screen: bool = False
    ring_zone: int | None = None  # via OpenRGB, when liquidctl has no LED channel
    ring_leds: int = 0
    gaps: list[str] = field(default_factory=list)

    def backend_for_color(self, channel: str) -> Backend:
        if channel in self.color_channels:
            return Backend.LIQUIDCTL
        if channel == "ring" and self.ring_zone is not None:
            return Backend.OPENRGB
        return Backend.NONE

    @property
    def color_targets(self) -> list[str]:
        targets = list(self.color_channels)
        if self.ring_zone is not None:
            targets.append("ring")
        return targets


def _is_base_impl(cls, name: str, *bases) -> bool:
    """True if `cls` inherits `name` unchanged from one of `bases`.

    liquidctl signals "unsupported" by inheriting a base method that raises
    NotSupportedByDevice, so identity against the base is the reliable test --
    short of calling the method, which for a write would change device state.
    """
    fn = getattr(cls, name, None)
    if fn is None:
        return True
    for base in bases:
        if fn is getattr(base, name, None):
            return True
    return False


def inspect_device(device) -> Capabilities:
    """Derive capabilities from an unconnected liquidctl driver instance."""
    from liquidctl.driver.kraken3 import KrakenX3
    from liquidctl.driver.smart_device import _BaseSmartDevice

    handle = device.handle
    cls = type(handle)
    caps = Capabilities()

    caps.speed_channels = sorted(getattr(handle, "_speed_channels", {}) or {})
    caps.color_channels = sorted(getattr(handle, "_color_channels", {}) or {})

    caps.speed_profile = not _is_base_impl(cls, "set_speed_profile", _BaseSmartDevice)
    if caps.speed_profile and issubclass(cls, KrakenX3):
        # Kraken curves interpolate against the block's liquid probe only; there
        # is no path to drive them from CPU or GPU temperature on-device.
        caps.profile_temp_source = "liquid"

    caps.screen = not _is_base_impl(cls, "set_screen", _BaseSmartDevice, KrakenX3)

    if not caps.color_channels and device.spec.ring_zone is not None:
        caps.ring_zone = device.spec.ring_zone

    caps.gaps = _describe_gaps(device, caps)
    return caps


def _describe_gaps(device, caps: Capabilities) -> list[str]:
    """Human-readable statements of what will *not* work, and the workaround."""
    gaps: list[str] = []

    if not caps.color_channels:
        if caps.ring_zone is not None:
            gaps.append(
                "liquidctl exposes no LED channel for this product; the pump "
                "light ring is driven via OpenRGB instead"
            )
        else:
            gaps.append("no LED control available from any backend")

    if not caps.speed_profile and caps.speed_channels:
        gaps.append(
            "no device-side fan curves; temperature-reactive control requires "
            "the fanctl daemon"
        )

    if caps.profile_temp_source == "liquid":
        gaps.append(
            "device-side curves follow liquid temperature only; CPU/GPU-driven "
            "curves require the fanctl daemon"
        )

    gaps.append("no non-volatile storage; all state is lost on power cycle")
    return gaps


def enrich_with_openrgb(device, caps: Capabilities, ring) -> None:
    """Fold live OpenRGB zone data into an already-derived Capabilities."""
    if ring is not None:
        caps.ring_leds = ring.leds
