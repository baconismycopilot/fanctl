"""Temperature sources for software-driven curves.

Everything here resolves hwmon by the `name` file, never by hwmon index.
Index numbering depends on probe order and genuinely changes across boots, so a
cached `/sys/class/hwmon/hwmon4` is a fan curve that silently follows the wrong
sensor after a reboot.

Paths are resolved once and re-resolved on read failure, which covers a device
disappearing and coming back.
"""

from __future__ import annotations

import ctypes
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

HWMON_ROOT = Path("/sys/class/hwmon")

# Plausible ranges, used to reject corrupt reads before they reach a curve.
#
# This is not paranoia: a status read issued straight after a burst of writes
# has returned a liquid temperature of 4C on this hardware, which is physically
# impossible for a running loop and would have driven a fan decision. hidraw
# offers no framing guarantee that a response belongs to the request you made.
#
# A bogus *high* reading is the dangerous direction on the way out -- it spins
# fans to maximum -- but a bogus low one during real load is worse, because it
# quietly under-cools.
PLAUSIBLE = {
    "liquid": (10.0, 80.0),
    "cpu": (5.0, 125.0),
    "gpu": (5.0, 125.0),
}


def is_plausible(source: str, value: float | None) -> bool:
    if value is None:
        return False
    low, high = PLAUSIBLE.get(source, (-273.0, 1000.0))
    return low <= value <= high


class TempError(RuntimeError):
    pass


def _hwmon_dirs(name: str) -> list[Path]:
    found = []
    for entry in sorted(HWMON_ROOT.glob("hwmon*")):
        try:
            if (entry / "name").read_text().strip() == name:
                found.append(entry)
        except OSError:
            continue
    return found


def _labelled_input(directory: Path, label: str) -> Path | None:
    """Find the temp*_input whose matching temp*_label reads `label`."""
    for label_file in sorted(directory.glob("temp*_label")):
        try:
            if label_file.read_text().strip().lower() == label.lower():
                candidate = Path(str(label_file).replace("_label", "_input"))
                if candidate.exists():
                    return candidate
        except OSError:
            continue
    return None


def _find_cpu() -> Path | None:
    for directory in _hwmon_dirs("k10temp"):
        # Tctl is the control temperature AMD's own fan logic uses.
        path = _labelled_input(directory, "Tctl")
        if path:
            return path
        fallback = directory / "temp1_input"
        if fallback.exists():
            return fallback
    return None


def _amdgpu_temp(directory: Path) -> Path | None:
    path = _labelled_input(directory, "edge") or (directory / "temp1_input")
    return path if path.exists() else None


def _find_discrete_amdgpu() -> Path | None:
    """A discrete AMD card's edge sensor.

    Discrete cards and the iGPU both register as `amdgpu`. Only discrete cards
    expose a `junction` (hotspot) sensor, which makes it a reliable
    discriminator.
    """
    for directory in _hwmon_dirs("amdgpu"):
        if _labelled_input(directory, "junction"):
            path = _amdgpu_temp(directory)
            if path:
                return path
    return None


def _find_any_amdgpu() -> Path | None:
    """Last resort: an iGPU, for a machine that has nothing else to follow."""
    for directory in _hwmon_dirs("amdgpu"):
        path = _amdgpu_temp(directory)
        if path:
            return path
    return None


class _Nvml:
    """NVIDIA GPU temperature through NVML, loaded with ctypes.

    The proprietary driver registers no hwmon device, so an RTX 5090 is
    invisible to everything above -- and before this existed, 'gpu' quietly
    resolved to the Ryzen iGPU's amdgpu sensor instead, which tracks nothing a
    case fan can affect. libnvidia-ml ships with the driver and needs no
    privilege to read temperatures, so it is loaded directly rather than adding
    a Python binding or shelling out to nvidia-smi every poll.

    NVML enumerates in PCI bus order, so index 0 is the same card every boot.
    """

    _TEMPERATURE_GPU = 0  # NVML_TEMPERATURE_GPU: the die sensor

    def __init__(self) -> None:
        self.name: str | None = None
        self._lib = None
        self._handle = ctypes.c_void_p()
        try:
            lib = ctypes.CDLL("libnvidia-ml.so.1")
            if lib.nvmlInit_v2() != 0:
                return
            if lib.nvmlDeviceGetHandleByIndex_v2(0, ctypes.byref(self._handle)) != 0:
                return
            buf = ctypes.create_string_buffer(96)
            if lib.nvmlDeviceGetName(self._handle, buf, len(buf)) == 0:
                self.name = buf.value.decode(errors="replace")
            self._lib = lib
        except (OSError, AttributeError):
            # No driver, or a library without these symbols: no NVIDIA source.
            self._lib = None

    @property
    def available(self) -> bool:
        return self._lib is not None

    def read(self) -> float | None:
        if self._lib is None:
            return None
        value = ctypes.c_uint()
        rc = self._lib.nvmlDeviceGetTemperature(
            self._handle, self._TEMPERATURE_GPU, ctypes.byref(value)
        )
        return float(value.value) if rc == 0 else None


@dataclass
class Sources:
    """Lazily-resolved temperature readers, shared across a daemon run."""

    device_reader: Callable[[], float | None] | None = None
    _paths: dict[str, Path | None] = field(default_factory=dict)
    _nvml: _Nvml | None = None

    def _sysfs(self, key: str, finder) -> float | None:
        if key not in self._paths:
            self._paths[key] = finder()
        path = self._paths[key]
        if path is None:
            return None
        try:
            return int(path.read_text().strip()) / 1000.0
        except (OSError, ValueError):
            # Re-resolve once: the device may have gone and come back.
            self._paths.pop(key, None)
            path = finder()
            self._paths[key] = path
            if path is None:
                return None
            try:
                return int(path.read_text().strip()) / 1000.0
            except (OSError, ValueError):
                return None

    def cpu(self) -> float | None:
        return self._sysfs("cpu", _find_cpu)

    def gpu(self) -> float | None:
        """The discrete card: AMD over hwmon, else NVIDIA over NVML, else an iGPU."""
        value = self._sysfs("gpu", _find_discrete_amdgpu)
        if value is not None or self._paths.get("gpu") is not None:
            return value
        if self._nvml is None:
            self._nvml = _Nvml()
        if self._nvml.available:
            return self._nvml.read()
        return self._sysfs("igpu", _find_any_amdgpu)

    def liquid(self) -> float | None:
        if self.device_reader is None:
            return None
        return self.device_reader()

    def read(self, source: str) -> float | None:
        """Read a source, rejecting implausible values.

        Returns None rather than a suspect number, and callers leave the channel
        at its current duty. Holding the last good duty for one poll is always
        safer than acting on a reading that cannot be true.
        """
        if source == "cpu":
            value = self.cpu()
        elif source == "gpu":
            value = self.gpu()
        elif source == "liquid":
            value = self.liquid()
            if not is_plausible(source, value):
                value = self.liquid()  # one retry: bus contention is transient
        else:
            raise TempError(f"unknown temperature source {source!r}")

        if not is_plausible(source, value):
            return None
        return value

    def describe(self) -> dict[str, str]:
        """Which sensor each source resolved to, for startup logging."""
        self.cpu()
        self.gpu()
        out = {}
        path = self._paths.get("cpu")
        out["cpu"] = str(path) if path else "unavailable"
        gpu_path = self._paths.get("gpu") or self._paths.get("igpu")
        if self._paths.get("gpu") is None and self._nvml is not None and self._nvml.available:
            out["gpu"] = f"nvml ({self._nvml.name or 'device 0'})"
        else:
            out["gpu"] = str(gpu_path) if gpu_path else "unavailable"
        out["liquid"] = "device" if self.device_reader else "unavailable"
        return out
