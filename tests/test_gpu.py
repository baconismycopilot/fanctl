"""GPU lighting: target naming, and 'fixed' on a card that has no Static mode.

The RTX 5090 FE reports three one-LED zones and only two modes, Off and Direct,
over OpenRGB. A profile wants 'fixed', and asking the card for a Static mode it
does not have would fail every apply, so 'fixed' is sent as Direct with the
colour written into each zone's LED buffer.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from openrgb.utils import DeviceType, ModeColors

from fancontrol import gpu
from fancontrol.backends import openrgb_be as be
from fancontrol.targets import TargetError


@dataclass
class FakeMode:
    name: str
    color_mode: ModeColors = ModeColors.NONE
    colors_min: int | None = None
    colors_max: int | None = None
    speed_min: int | None = None
    speed_max: int | None = None
    speed: int | None = None
    direction: int | None = None
    colors: list = field(default_factory=list)


@dataclass
class FakeZone:
    id: int
    name: str
    leds: list
    written: list = field(default_factory=list)

    def set_colors(self, colors):
        self.written = colors


@dataclass
class FakeMeta:
    location: str = ""
    serial: str = ""


@dataclass
class FakeDevice:
    id: int
    name: str
    type: DeviceType
    zones: list
    modes: list = field(default_factory=list)
    metadata: FakeMeta = field(default_factory=FakeMeta)
    active: FakeMode | None = None

    def set_mode(self, mode):
        self.active = mode


class FakeClient:
    def __init__(self, devices):
        self.devices = devices


def founders_edition(device_id: int = 0) -> FakeDevice:
    return FakeDevice(
        id=device_id,
        name="NVIDIA GeForce RTX 5090 FE",
        type=DeviceType.GPU,
        zones=[FakeZone(id=i, name=f"{i} - SINGLE COLOR", leds=[object()]) for i in range(3)],
        modes=[FakeMode("Off"), FakeMode("Direct", ModeColors.PER_LED)],
    )


def ring_device(device_id: int) -> FakeDevice:
    return FakeDevice(
        id=device_id,
        name="NZXT Kraken",
        type=DeviceType.COOLER,
        zones=[FakeZone(id=0, name="Ring", leds=[object()] * 24)],
        modes=[FakeMode("Static", ModeColors.MODE_SPECIFIC)],
    )


# ------------------------------------------------------------------ tokens


@pytest.mark.parametrize("token", ["gpu", "GPU", "gpu:2", "graphics", "gpu:all"])
def test_gpu_tokens_are_recognised(token):
    assert gpu.is_gpu(token)


@pytest.mark.parametrize("token", ["ring", "hub:2", "memory", "gpus"])
def test_other_targets_are_left_alone(token):
    assert not gpu.is_gpu(token)


@pytest.mark.parametrize("token", ["gpu:0", "gpu:top", "gpu:-1"])
def test_malformed_index_is_refused(token):
    with pytest.raises(TargetError):
        gpu._index(token)


# --------------------------------------------------------------- discovery


def test_only_gpu_zones_are_found_in_zone_order():
    client = FakeClient([ring_device(0), founders_edition(1)])
    zones = gpu.discover(client)
    assert [z.label for z in zones] == ["gpu:1", "gpu:2", "gpu:3"]
    assert [z.zone.zone_id for z in zones] == [0, 1, 2]


def test_select_one_zone_or_all():
    zones = gpu.discover(FakeClient([founders_edition()]))
    assert gpu.select(zones, "gpu") == zones
    assert [z.label for z in gpu.select(zones, "gpu:2")] == ["gpu:2"]
    with pytest.raises(TargetError):
        gpu.select(zones, "gpu:4")


def test_no_gpu_lighting_is_an_error_not_a_no_op():
    with pytest.raises(TargetError):
        gpu.select([], "gpu")


# ------------------------------------------------------------------- modes


def test_fixed_is_offered_and_sent_as_direct_without_static():
    zone = gpu.discover(FakeClient([founders_edition()]))[0]
    assert gpu.modes_for(zone) == ["direct", "fixed", "off"]
    assert gpu.native_mode(zone, "fixed") == "direct"
    assert gpu.native_mode(zone, "off") == "off"


def test_a_card_with_static_keeps_fixed_as_static():
    card = founders_edition()
    card.modes.append(FakeMode("Static", ModeColors.MODE_SPECIFIC))
    zone = gpu.discover(FakeClient([card]))[0]
    assert gpu.native_mode(zone, "fixed") == "fixed"


def test_direct_write_lands_in_the_led_buffer():
    card = founders_edition()
    client = FakeClient([card])
    zone = gpu.discover(client)[0]

    be.set_zone(client, zone.zone, "Direct", [[0, 204, 255]])

    assert card.active is not None and card.active.name == "Direct"
    written = card.zones[0].written
    assert [(c.red, c.green, c.blue) for c in written] == [(0, 204, 255)]
