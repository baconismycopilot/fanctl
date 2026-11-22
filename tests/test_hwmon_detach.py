"""With nzxt_kraken3 bound, liquidctl writes duty through root-only hwmon sysfs.

pwm1 is 0644 root and the curve points are 0200 root, so a rootless write fails
with EACCES. fanctl drops the hwmon handle in that case so liquidctl falls back
to hidraw, which uaccess covers. A writable hwmon is left alone.
"""

from types import SimpleNamespace

from fancontrol.backends.liquidctl_be import _detach_readonly_hwmon


def _handle(tmp_path, mode):
    pwm = tmp_path / "pwm1"
    pwm.write_text("51")
    pwm.chmod(mode)
    return SimpleNamespace(_hwmon=SimpleNamespace(path=tmp_path))


def test_readonly_hwmon_is_detached(tmp_path):
    handle = _handle(tmp_path, 0o444)
    _detach_readonly_hwmon(handle)
    assert handle._hwmon is None


def test_writable_hwmon_is_kept(tmp_path):
    handle = _handle(tmp_path, 0o644)
    _detach_readonly_hwmon(handle)
    assert handle._hwmon is not None


def test_device_without_kernel_driver_is_untouched():
    handle = SimpleNamespace(_hwmon=None)
    _detach_readonly_hwmon(handle)
    assert handle._hwmon is None
