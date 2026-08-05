"""liquidctl backend -- fans, pump, curves, LCD, and hub RGB.

liquidctl is imported as a library rather than driven as a subprocess. The CLI
costs a full device enumeration per invocation and reports failures as text on
stderr; importing gives us real exceptions and lets the daemon hold handles open
across polls.

The trade is that we depend on liquidctl's internal driver API, which carries no
stability guarantee. That dependency is deliberately confined to this module and
caps.py so a breaking upgrade has a small blast radius.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass

from ..lock import device_lock


@dataclass
class Reading:
    key: str
    value: object
    unit: str

    @property
    def is_empty_channel(self) -> bool:
        """Heuristic: a speed reading of exactly 0 rpm means nothing plugged in."""
        return self.unit == "rpm" and self.value == 0


@contextmanager
def opened(device):
    """Connect a device for the duration of a block, serialised by its lock."""
    with device_lock(device.serial):
        device.handle.connect()
        try:
            yield device.handle
        finally:
            try:
                device.handle.disconnect()
            except Exception:  # noqa: BLE001 - disconnect failures are not actionable
                pass


def read_status(device) -> list[Reading]:
    with opened(device) as handle:
        raw = handle.get_status()
    return [Reading(key=k, value=v, unit=u) for k, v, u in raw]


def initialize(device) -> list[Reading]:
    """Initialise a device and report what it found.

    Required after a cold boot or a hardware change before LED accessories are
    addressable, and the only way to learn which accessory sits on which channel.
    """
    with opened(device) as handle:
        raw = handle.initialize()
    return [Reading(key=k, value=v, unit=u) for k, v, u in (raw or [])]


def set_fixed_speed(device, channel: str, duty: int) -> None:
    with opened(device) as handle:
        handle.set_fixed_speed(channel, duty)


def set_speed_profile(device, channel: str, profile: list[tuple[int, int]]) -> None:
    """Write a device-side duty curve. Only the Kraken supports this."""
    with opened(device) as handle:
        handle.set_speed_profile(channel, profile)


def set_color(
    device,
    channel: str,
    mode: str,
    colors: list[list[int]],
    speed: str = "normal",
    direction: str = "forward",
) -> None:
    with opened(device) as handle:
        handle.set_color(channel, mode, colors, speed=speed, direction=direction)


def set_screen(device, mode: str, value: str | None) -> None:
    with opened(device) as handle:
        handle.set_screen("lcd", mode, value)
