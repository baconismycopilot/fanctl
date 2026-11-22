"""Which sensor the 'gpu' temperature source follows.

The hardware fact: this machine has a Ryzen iGPU (amdgpu, no junction sensor)
and an RTX 5090 on the proprietary driver, which registers no hwmon device at
all. Resolving 'gpu' from hwmon alone therefore picked the iGPU -- a curve keyed
on it would have followed a sensor nothing in the case can cool. A discrete AMD
card still wins when present; NVML comes next; the iGPU only as a last resort.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fancontrol import temps


def hwmon(root: Path, index: int, name: str, sensors: dict[str, int]) -> None:
    d = root / f"hwmon{index}"
    d.mkdir()
    (d / "name").write_text(name + "\n")
    for i, (label, millideg) in enumerate(sensors.items(), start=1):
        (d / f"temp{i}_label").write_text(label + "\n")
        (d / f"temp{i}_input").write_text(f"{millideg}\n")


class FakeNvml:
    def __init__(self, value: float | None):
        self.value = value
        self.name = "NVIDIA GeForce RTX 5090"

    @property
    def available(self) -> bool:
        return self.value is not None

    def read(self) -> float | None:
        return self.value


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setattr(temps, "HWMON_ROOT", tmp_path)
    return tmp_path


def test_nvidia_beats_the_igpu(root, monkeypatch):
    hwmon(root, 2, "amdgpu", {"edge": 38000})  # iGPU: no junction
    monkeypatch.setattr(temps, "_Nvml", lambda: FakeNvml(61.0))
    sources = temps.Sources()
    assert sources.read("gpu") == 61.0
    assert sources.describe()["gpu"].startswith("nvml")


def test_discrete_amd_beats_nvidia(root, monkeypatch):
    hwmon(root, 2, "amdgpu", {"edge": 38000})
    hwmon(root, 5, "amdgpu", {"edge": 55000, "junction": 70000})
    monkeypatch.setattr(temps, "_Nvml", lambda: FakeNvml(61.0))
    assert temps.Sources().read("gpu") == 55.0


def test_igpu_only_as_a_last_resort(root, monkeypatch):
    hwmon(root, 2, "amdgpu", {"edge": 38000})
    monkeypatch.setattr(temps, "_Nvml", lambda: FakeNvml(None))
    assert temps.Sources().read("gpu") == 38.0


def test_failed_nvml_read_is_not_a_temperature(root, monkeypatch):
    class Flaky(FakeNvml):
        def read(self):
            return None

    monkeypatch.setattr(temps, "_Nvml", lambda: Flaky(61.0))
    assert temps.Sources().read("gpu") is None
