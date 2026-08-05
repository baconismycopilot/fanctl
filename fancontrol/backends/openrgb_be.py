"""OpenRGB backend -- pump light ring only.

The Kraken 2024 Elite's light ring is unreachable through liquidctl: kraken3.py
maps product 0x3012 to an empty colour-channel dict, and the upstream guide
states plainly that controlling the ring is not yet supported. OpenRGB drives it
as a generic Hue 2 device on the same hidraw node, so that is the only thing this
backend is responsible for.

We speak to the SDK server rather than shelling out to the openrgb binary: the
binary re-runs full device detection (~2s) and re-grabs every hidraw node on each
invocation, which is unusable from a polling daemon.
"""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_PORT = 6742


class OpenRGBUnavailable(RuntimeError):
    """The SDK server is not reachable. Callers degrade rather than fail."""


@dataclass
class Ring:
    device_id: int
    zone_id: int
    zone_name: str
    leds: int
    modes: list[str]


def connect(port: int = DEFAULT_PORT, name: str = "fanctl"):
    try:
        from openrgb import OpenRGBClient

        return OpenRGBClient(port=port, name=name)
    except Exception as exc:  # noqa: BLE001 - any failure means "degrade"
        raise OpenRGBUnavailable(
            f"OpenRGB SDK server not reachable on port {port}: {exc}\n"
            "  start it with: systemctl --user start openrgb-server"
        ) from exc


def _find_mode(device, name: str):
    for mode in device.modes:
        if mode.name.strip().lower() == name.strip().lower():
            return mode
    available = ", ".join(m.name for m in device.modes)
    raise OpenRGBUnavailable(f"ring has no mode {name!r}; available: {available}")


def _apply_colors(mode, colors: list[list[int]]) -> None:
    """Attach colours to a mode, respecting its declared min/max.

    Mode colour handling is per-mode metadata, not a device-wide property:
    Spectrum Cycle takes none, Static takes exactly one, Fading takes up to
    eight. Reading it off the mode avoids sending colours a mode will discard.
    """
    from openrgb.utils import ModeColors, RGBColor

    if mode.color_mode in (ModeColors.NONE, None):
        return
    if not colors:
        return

    lo = mode.colors_min or 1
    hi = mode.colors_max or len(colors)
    if len(colors) < lo:
        raise OpenRGBUnavailable(
            f"mode {mode.name!r} needs at least {lo} colour(s), got {len(colors)}"
        )
    mode.colors = [RGBColor(*c) for c in colors[:hi]]


def set_ring(
    client,
    ring: "Ring",
    mode_name: str | None,
    colors: list[list[int]],
    speed: int | None = None,
    direction: int | None = None,
) -> None:
    """Drive the pump light ring.

    `mode_name` of None means the canonical 'off': the ring exposes no Off mode,
    so it is emulated by holding every LED black under per-LED control.
    """
    from openrgb.utils import RGBColor

    device = client.devices[ring.device_id]
    zone = device.zones[ring.zone_id]

    if mode_name is None:
        device.set_custom_mode()
        zone.set_colors([RGBColor(0, 0, 0)] * max(len(zone.leds), 1))
        return

    mode = _find_mode(device, mode_name)
    _apply_colors(mode, colors)

    if speed is not None and mode.speed_min is not None:
        mode.speed = max(mode.speed_min, min(mode.speed_max, speed))
    if direction is not None and mode.direction is not None:
        mode.direction = direction

    device.set_mode(mode)


def find_ring(client, serial: str, zone_index: int) -> Ring | None:
    """Locate a device's ring zone by serial number.

    Matching on serial rather than name or index keeps this stable across
    OpenRGB's own detection order, which is not guaranteed between releases.
    """
    for dev in client.devices:
        if (dev.metadata.serial or "").strip().upper() != serial.strip().upper():
            continue
        if zone_index >= len(dev.zones):
            return None
        zone = dev.zones[zone_index]
        return Ring(
            device_id=dev.id,
            zone_id=zone.id,
            zone_name=zone.name,
            leds=len(zone.leds),
            modes=[m.name for m in dev.modes],
        )
    return None
