"""OpenRGB backend -- the lighting liquidctl cannot reach.

Two unrelated things land here for the same reason.

The Kraken 2024 Elite's light ring is unreachable through liquidctl: kraken3.py
maps product 0x3012 to an empty colour-channel dict, and the upstream guide
states plainly that controlling the ring is not yet supported. OpenRGB drives it
as a generic Hue 2 device on the same hidraw node.

RGB memory has no liquidctl presence at all -- it is an ENE controller on the
SMBus behind the DIMMs' SPD addresses, not a USB device -- so OpenRGB is the
only backend that will ever see it.

We speak to the SDK server rather than shelling out to the openrgb binary: the
binary re-runs full device detection (~2s) and re-grabs every hidraw node on each
invocation, which is unusable from a polling daemon.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

DEFAULT_PORT = 6742

_ADDRESS = re.compile(r"address\s+(0x[0-9a-fA-F]+)")


class OpenRGBUnavailable(RuntimeError):
    """The SDK server is not reachable. Callers degrade rather than fail."""


@dataclass
class Zone:
    """One addressable zone of an OpenRGB device."""

    device_id: int
    zone_id: int
    zone_name: str
    device_name: str
    location: str
    leds: int
    modes: list[str]

    @property
    def address(self) -> str | None:
        """SMBus address from the location string, for I2C devices."""
        match = _ADDRESS.search(self.location or "")
        return match.group(1).lower() if match else None


def connect(port: int = DEFAULT_PORT, name: str = "fanctl"):
    try:
        from openrgb import OpenRGBClient

        return OpenRGBClient(port=port, name=name)
    except Exception as exc:
        raise OpenRGBUnavailable(
            f"OpenRGB SDK server not reachable on port {port}: {exc}\n"
            "  start it with: systemctl --user start openrgb-server"
        ) from exc


def _zone(dev, zone) -> Zone:
    return Zone(
        device_id=dev.id,
        zone_id=zone.id,
        zone_name=zone.name,
        device_name=dev.name,
        location=dev.metadata.location or "",
        leds=len(zone.leds),
        modes=[m.name for m in dev.modes],
    )


def find_ring(client, serial: str, zone_index: int) -> Zone | None:
    """Locate a device's ring zone by serial number.

    Matching on serial rather than name or index keeps this stable across
    OpenRGB's own detection order, which is not guaranteed between releases.
    """
    for dev in client.devices:
        if (dev.metadata.serial or "").strip().upper() != serial.strip().upper():
            continue
        if zone_index >= len(dev.zones):
            return None
        return _zone(dev, dev.zones[zone_index])
    return None


def find_memory(client) -> list[Zone]:
    """Every DRAM lighting zone, in SMBus address order.

    DIMMs report no serial, so the ordering key has to come from somewhere else.
    The SMBus address is a property of the slot the stick sits in, so sorting on
    it means stick 1 stays stick 1 across reboots and OpenRGB upgrades -- which
    plain detection order would not.
    """
    from openrgb.utils import DeviceType

    zones = [
        _zone(dev, zone)
        for dev in client.devices
        if dev.type == DeviceType.DRAM
        for zone in dev.zones
        if zone.leds
    ]
    return sorted(zones, key=lambda z: (z.address or "", z.device_id, z.zone_id))


def _find_mode(zone: Zone, device, name: str):
    for mode in device.modes:
        if mode.name.strip().lower() == name.strip().lower():
            return mode
    available = ", ".join(m.name for m in device.modes)
    raise OpenRGBUnavailable(f"{zone.zone_name} has no mode {name!r}; available: {available}")


def _scale_speed(mode, index: int, steps: int) -> int | None:
    """Map a canonical speed index onto a mode's raw speed range.

    The range is not always ascending: the Hue 2 ring numbers 0 slowest to 4
    fastest, while the ENE DRAM controller runs 4 down to 0. Interpolating
    between the declared endpoints in the order given handles both, where
    clamping to min/max would pin every DRAM animation to one speed.
    """
    lo, hi = mode.speed_min, mode.speed_max
    if lo is None or hi is None:
        return None
    if steps < 2:
        return lo
    return round(lo + (hi - lo) * index / (steps - 1))


def _apply_colors(zone_handle, mode, colors: list[list[int]]) -> None:
    """Attach colours wherever this mode reads them from.

    Where the colours live is per-mode metadata, not a device-wide property.
    MODE_SPECIFIC modes carry their own palette (the ring's Static takes one
    colour, its Fading up to eight); PER_LED modes ignore that and read the
    zone's LED buffer, which is how every ENE DRAM mode works. Writing to the
    wrong one is silently accepted and does nothing.
    """
    from openrgb.utils import ModeColors, RGBColor

    if mode.color_mode in (ModeColors.NONE, None) or not colors:
        return

    if mode.color_mode == ModeColors.PER_LED:
        # Fewer colours than LEDs is not an error: cycling them across the zone
        # turns 'fixed ff0000 00ff00' into a repeating pattern down the stick.
        count = max(len(zone_handle.leds), 1)
        zone_handle.set_colors([RGBColor(*colors[i % len(colors)]) for i in range(count)])
        return

    lo = mode.colors_min or 1
    hi = mode.colors_max or len(colors)
    if len(colors) < lo:
        raise OpenRGBUnavailable(
            f"mode {mode.name!r} needs at least {lo} colour(s), got {len(colors)}"
        )
    mode.colors = [RGBColor(*c) for c in colors[:hi]]


def set_zone(
    client,
    zone: Zone,
    mode_name: str | None,
    colors: list[list[int]],
    speed: int | None = None,
    speed_steps: int = 5,
    direction: int | None = None,
) -> None:
    """Drive one lighting zone.

    `mode_name` of None means the canonical 'off'. The DRAM controller has a
    real Off mode; the ring does not, and there it is emulated by holding every
    LED black under per-LED control.
    """
    from openrgb.utils import RGBColor

    device = client.devices[zone.device_id]
    zone_handle = device.zones[zone.zone_id]

    if mode_name is None:
        for mode in device.modes:
            if mode.name.strip().lower() == "off":
                device.set_mode(mode)
                return
        device.set_custom_mode()
        zone_handle.set_colors([RGBColor(0, 0, 0)] * max(len(zone_handle.leds), 1))
        return

    mode = _find_mode(zone, device, mode_name)
    # Colours first, mode second. A PER_LED write goes to the zone's LED buffer,
    # which the mode then reads; sending it after set_mode works too, but this
    # order means one packet ordering for both colour models.
    _apply_colors(zone_handle, mode, colors)

    if speed is not None:
        scaled = _scale_speed(mode, speed, speed_steps)
        if scaled is not None:
            mode.speed = scaled
    if direction is not None and mode.direction is not None:
        mode.direction = direction

    device.set_mode(mode)
