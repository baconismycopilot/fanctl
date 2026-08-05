"""Device discovery and stable aliasing.

Devices are keyed by serial number, never by enumeration order: replugging an
unrelated USB device reshuffles liquidctl's device list, and an alias that moves
is worse than no alias at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

NZXT = 0x1E71


@dataclass(frozen=True)
class DeviceSpec:
    """Static facts about a supported product."""

    alias: str
    vendor: int
    product: int
    label: str
    # OpenRGB zone index holding the pump light ring, for products where
    # liquidctl exposes no LED channel at all. See caps.py for why.
    ring_zone: int | None = None


REGISTRY: dict[int, DeviceSpec] = {
    0x3012: DeviceSpec("kraken", NZXT, 0x3012, "Kraken 2024 Elite RGB", ring_zone=0),
    0x2022: DeviceSpec("hub", NZXT, 0x2022, "Control Hub"),
}


@dataclass
class Device:
    """A discovered device: a liquidctl handle plus the identity we resolved."""

    alias: str
    spec: DeviceSpec
    serial: str
    handle: Any  # liquidctl driver instance

    @property
    def label(self) -> str:
        return self.spec.label

    @property
    def hidraw(self) -> str | None:
        # hidapi reports the path as bytes; decode for display and for use as a
        # lock-file key.
        path = getattr(self.handle.device, "path", None)
        if isinstance(path, bytes):
            return path.decode("utf-8", "replace")
        return path


class DeviceError(RuntimeError):
    pass


def _alias_for(spec: DeviceSpec, taken: set[str]) -> str:
    """Disambiguate duplicate products as kraken, kraken2, kraken3..."""
    if spec.alias not in taken:
        return spec.alias
    n = 2
    while f"{spec.alias}{n}" in taken:
        n += 1
    return f"{spec.alias}{n}"


def discover() -> list[Device]:
    """Find every supported device, sorted by alias for stable output."""
    from liquidctl.driver import find_liquidctl_devices

    found: list[Device] = []
    taken: set[str] = set()

    for handle in find_liquidctl_devices():
        product = getattr(handle.device, "product_id", None)
        spec = REGISTRY.get(product)
        if spec is None or getattr(handle.device, "vendor_id", None) != spec.vendor:
            continue
        serial = handle.device.serial_number or "unknown"
        alias = _alias_for(spec, taken)
        taken.add(alias)
        found.append(Device(alias=alias, spec=spec, serial=serial, handle=handle))

    return sorted(found, key=lambda d: d.alias)


def resolve(devices: list[Device], alias: str) -> Device:
    for dev in devices:
        if dev.alias == alias:
            return dev
    known = ", ".join(d.alias for d in devices) or "none found"
    raise DeviceError(f"no device with alias {alias!r} (known: {known})")
