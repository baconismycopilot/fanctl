"""RGB memory: target naming, ordering, and the two OpenRGB quirks it exposed.

The DRAM controller disagrees with the pump ring on both of the things
openrgb_be had previously only seen one answer for -- where a mode's colours
live, and which way its speed scale runs -- so those are covered here against
both shapes rather than only the one the hardware in front of us has.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from openrgb.utils import DeviceType, ModeColors, RGBColor

from fancontrol import memory as mem
from fancontrol.backends import openrgb_be as be
from fancontrol.cli import build_parser
from fancontrol.targets import TargetError

DIMM_LOCATION = "I2C: SMBus PIIX4 adapter port 0 at 0b00 (/dev/i2c-17), address 0x{:02x}"


# ------------------------------------------------------------------- fakes


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
    custom: bool = False

    def set_mode(self, mode):
        self.active = mode

    def set_custom_mode(self):
        self.custom = True


class FakeClient:
    def __init__(self, devices):
        self.devices = devices


def dimm(device_id: int, address: int, leds: int = 8) -> FakeDevice:
    return FakeDevice(
        id=device_id,
        name="ENE DRAM",
        type=DeviceType.DRAM,
        zones=[FakeZone(id=0, name="DRAM", leds=[object()] * leds)],
        modes=[FakeMode("Direct"), FakeMode("Off"), FakeMode("Static", ModeColors.PER_LED)],
        metadata=FakeMeta(location=DIMM_LOCATION.format(address)),
    )


# ------------------------------------------------------------------ tokens


@pytest.mark.parametrize("token", ["memory", "mem", "ram", "dram", "MEMORY", "memory:2", "ram:1"])
def test_memory_tokens_are_recognised(token):
    assert mem.is_memory(token)


@pytest.mark.parametrize("token", ["ring", "hub:2", "hub:led2", "sync", "memorial"])
def test_other_targets_are_left_alone(token):
    assert not mem.is_memory(token)


@pytest.mark.parametrize("token", ["memory", "memory:all", "ram:sync"])
def test_bare_token_means_every_stick(token):
    assert mem._index(token) is None


@pytest.mark.parametrize(("token", "expected"), [("memory:1", 1), ("memory:2", 2), ("mem:11", 11)])
def test_indexed_token(token, expected):
    assert mem._index(token) == expected


@pytest.mark.parametrize("token", ["memory:left", "memory:0", "memory:-1", "memory:1.5"])
def test_malformed_index_is_refused(token):
    # Silently treating 'memory:0' as 'every stick' would set the wrong DIMMs.
    with pytest.raises(TargetError):
        mem._index(token)


# ---------------------------------------------------------------- ordering


def test_sticks_are_numbered_by_smbus_address_not_detection_order():
    # OpenRGB's device order is not guaranteed; the SMBus address is a property
    # of the slot, so 'memory:1' has to follow the address.
    client = FakeClient([dimm(0, 0x73), dimm(1, 0x71)])
    sticks = mem.discover(client)
    assert [s.address for s in sticks] == ["0x71", "0x73"]
    assert [s.label for s in sticks] == ["memory:1", "memory:2"]
    assert [s.zone.device_id for s in sticks] == [1, 0]


def test_non_dram_devices_and_empty_zones_are_ignored():
    keyboard = FakeDevice(0, "Wooting", DeviceType.KEYBOARD, [FakeZone(0, "keys", [object()])])
    empty = dimm(2, 0x75)
    empty.zones[0].leds = []
    client = FakeClient([keyboard, dimm(1, 0x71), empty])
    assert [s.address for s in mem.discover(client)] == ["0x71"]


def test_address_is_parsed_out_of_the_location_string():
    zone = be.find_memory(FakeClient([dimm(0, 0x71)]))[0]
    assert zone.address == "0x71"
    assert be.Zone(0, 0, "z", "d", "HID: /dev/hidraw14", 24, []).address is None


# --------------------------------------------------------------- selection


def test_select_all_and_one():
    sticks = mem.discover(FakeClient([dimm(0, 0x71), dimm(1, 0x73)]))
    assert len(mem.select(sticks, "memory")) == 2
    assert mem.select(sticks, "memory:2")[0].address == "0x73"


def test_select_reports_rather_than_silently_doing_nothing():
    sticks = mem.discover(FakeClient([dimm(0, 0x71)]))
    with pytest.raises(TargetError, match="found 1 stick"):
        mem.select(sticks, "memory:2")
    with pytest.raises(TargetError, match="SMBus"):
        mem.select([], "memory")


def test_modes_are_translated_into_canonical_names():
    stick = mem.discover(FakeClient([dimm(0, 0x71)]))[0]
    assert mem.modes_for(stick) == ["direct", "fixed", "off"]


# ------------------------------------------------------------- speed scale


@pytest.mark.parametrize(
    ("lo", "hi", "expected"),
    [
        (0, 4, [0, 1, 2, 3, 4]),  # Hue 2 ring: ascending
        (4, 0, [4, 3, 2, 1, 0]),  # ENE DRAM: descending
        (0, 2, [0, 0, 1, 2, 2]),  # narrower range than we have names for
    ],
)
def test_speed_maps_onto_ranges_running_either_way(lo, hi, expected):
    # Clamping to (min, max) pinned every DRAM animation to one speed, because
    # the controller declares min=4 max=0 and max(4, min(0, x)) is always 4.
    mode = FakeMode("Chase", speed_min=lo, speed_max=hi)
    assert [be._scale_speed(mode, i, 5) for i in range(5)] == expected


def test_speed_is_left_alone_when_the_mode_has_no_range():
    assert be._scale_speed(FakeMode("Static"), 2, 5) is None


# ------------------------------------------------------------ colour paths


def test_per_led_colours_go_to_the_zone_buffer():
    zone = FakeZone(0, "DRAM", [object()] * 8)
    be._apply_colors(zone, FakeMode("Static", ModeColors.PER_LED), [[255, 0, 0]])
    assert zone.written == [RGBColor(255, 0, 0)] * 8


def test_per_led_colours_cycle_when_there_are_fewer_than_leds():
    zone = FakeZone(0, "DRAM", [object()] * 4)
    be._apply_colors(zone, FakeMode("Static", ModeColors.PER_LED), [[1, 0, 0], [0, 2, 0]])
    assert zone.written == [RGBColor(1, 0, 0), RGBColor(0, 2, 0)] * 2


def test_mode_specific_colours_go_to_the_mode():
    # The ring's model: writing these to the LED buffer instead is accepted and
    # does nothing, which is why the two are kept apart.
    zone = FakeZone(0, "ring", [object()] * 24)
    mode = FakeMode("Static", ModeColors.MODE_SPECIFIC, colors_min=1, colors_max=1)
    be._apply_colors(zone, mode, [[0, 0, 255], [9, 9, 9]])
    assert mode.colors == [RGBColor(0, 0, 255)]
    assert zone.written == []


def test_modes_taking_no_colours_get_none():
    zone = FakeZone(0, "DRAM", [object()] * 8)
    mode = FakeMode("Spectrum Cycle", ModeColors.NONE)
    be._apply_colors(zone, mode, [[255, 0, 0]])
    assert mode.colors == [] and zone.written == []


def test_too_few_colours_for_the_mode_is_an_error():
    mode = FakeMode("Alternating", ModeColors.MODE_SPECIFIC, colors_min=2, colors_max=2)
    with pytest.raises(be.OpenRGBUnavailable, match="at least 2"):
        be._apply_colors(FakeZone(0, "ring", []), mode, [[1, 1, 1]])


# ----------------------------------------------------------------- writing


def test_off_prefers_a_real_off_mode():
    device = dimm(0, 0x71)
    client = FakeClient([device])
    be.set_zone(client, mem.discover(client)[0].zone, None, [])
    assert device.active.name == "Off"
    assert not device.custom


def test_off_falls_back_to_black_when_there_is_no_off_mode():
    # The Hue 2 ring exposes no Off; holding every LED black is the only way.
    ring = FakeDevice(
        0,
        "ring",
        DeviceType.LEDSTRIP,
        [FakeZone(0, "Hue 2 Channel 1", [object()] * 3)],
        modes=[FakeMode("Direct"), FakeMode("Static", ModeColors.MODE_SPECIFIC)],
    )
    be.set_zone(FakeClient([ring]), be.Zone(0, 0, "z", "ring", "HID:", 3, []), None, [])
    assert ring.custom
    assert ring.zones[0].written == [RGBColor(0, 0, 0)] * 3


def test_setting_a_mode_writes_colours_then_switches():
    device = dimm(0, 0x71)
    client = FakeClient([device])
    be.set_zone(client, mem.discover(client)[0].zone, "Static", [[0, 204, 255]])
    assert device.active.name == "Static"
    assert device.zones[0].written == [RGBColor(0, 204, 255)] * 8


def test_unknown_mode_names_the_alternatives():
    device = dimm(0, 0x71)
    client = FakeClient([device])
    with pytest.raises(be.OpenRGBUnavailable, match="Direct, Off, Static"):
        be.set_zone(client, mem.discover(client)[0].zone, "Nonsense", [])


# -------------------------------------------------------------- cli wiring


def test_led_set_accepts_memory_targets():
    args = build_parser().parse_args(["led", "set", "memory:1", "fixed", "ff0000"])
    assert args.target == "memory:1"
    assert mem.is_memory(args.target)
