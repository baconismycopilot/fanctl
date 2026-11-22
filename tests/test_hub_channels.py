"""The Control Hub selects an LED channel by bitmask: led(n) is 1 << (n - 1).

liquidctl 1.16.0 maps led2 to 0x04 and led3 to 0x06, so a colour written to
led2 lit nothing on this hub while led4 (0x08, correct by coincidence) worked.
Sending 0x02 lit the F240 RGB Core on led2, confirmed by eye.
"""

from liquidctl.driver.control_hub import ControlHub

from fancontrol.backends.liquidctl_be import _HUB_CHANNEL_BYTES, _fix_hub_channel_bytes


def _bare_hub():
    # Only the class and the class-level map matter; skip the USB constructor.
    return ControlHub.__new__(ControlHub)


def test_led_channels_are_one_bit_each():
    assert _HUB_CHANNEL_BYTES == {i: 1 << i for i in range(5)}


def test_hub_handle_gets_the_bitmask_map():
    hub = _bare_hub()
    _fix_hub_channel_bytes(hub)
    assert hub._CHANNEL_BYTE_MAP[1] == 0x02


def test_upstream_map_still_needs_the_fix():
    # If this fails, liquidctl fixed the map upstream and the override can go.
    assert ControlHub._CHANNEL_BYTE_MAP[1] != 0x02


def test_other_drivers_are_untouched():
    class Kraken:
        pass

    k = Kraken()
    k._CHANNEL_BYTE_MAP = {1: 0x04}
    _fix_hub_channel_bytes(k)
    assert k._CHANNEL_BYTE_MAP == {1: 0x04}
