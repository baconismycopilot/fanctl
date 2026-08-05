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
