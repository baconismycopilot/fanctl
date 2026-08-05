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
