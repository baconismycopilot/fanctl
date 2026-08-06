"""Scheduling the pump from the daemon.

The pump reached the daemon because measurement said its lever is the
CPU-to-coolant delta (2.6C between 30% and 100%) rather than coolant
temperature, and the Kraken can only interpolate a curve against its own liquid
probe. So which curves stay on-device and which get scheduled is the thing to
pin down here.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from fancontrol import caps as caps_mod
from fancontrol import config as cfg_mod
from fancontrol import daemon as daemon_mod
from fancontrol.backends import liquidctl_be


@dataclass
class FakeTarget:
    channel: str
    caps: caps_mod.Capabilities
    device: object = None

    def __str__(self) -> str:
        return f"kraken:{self.channel}"


def kraken_pump_caps() -> caps_mod.Capabilities:
    # The real shape: it runs curves itself, but only against liquid.
    return caps_mod.Capabilities(
        speed_channels=["fan", "pump"], speed_profile=True, profile_temp_source="liquid"
    )


def hub_fan_caps() -> caps_mod.Capabilities:
    return caps_mod.Capabilities(speed_channels=["fan2"], speed_profile=False)


def make_daemon(**daemon_kwargs) -> daemon_mod.Daemon:
    config = cfg_mod.Config(daemon=cfg_mod.DaemonSettings(**daemon_kwargs))
    return daemon_mod.Daemon(config=config, profile=cfg_mod.Profile(name="t"))


def pump(source: str, curve=((50, 40), (85, 85))) -> cfg_mod.FanSetting:
    return cfg_mod.FanSetting(channel="pump", label="pump", curve=list(curve), source=source)


# ------------------------------------------------------- what gets scheduled


def test_a_cpu_keyed_pump_curve_is_scheduled_here():
    # The Kraken cannot follow CPU temperature, so without this the curve is
    # skipped by apply and then run by nobody.
    d = make_daemon()
    d._manage(pump("cpu"), FakeTarget("pump", kraken_pump_caps()))
    assert [e.target.channel for e in d.managed] == ["pump"]
    assert d.managed[0].source == "cpu"


def test_a_liquid_keyed_pump_curve_stays_on_the_cooler():
    d = make_daemon()
    d._manage(pump("liquid"), FakeTarget("pump", kraken_pump_caps()))
    assert d.managed == []


def test_a_fixed_pump_duty_is_not_a_curve():
    d = make_daemon()
    d._manage(
        cfg_mod.FanSetting(channel="pump", label="pump", duty=60),
        FakeTarget("pump", kraken_pump_caps()),
    )
    assert d.managed == []


def test_hub_fans_are_scheduled_whatever_they_follow():
    d = make_daemon()
    for source in ("liquid", "cpu"):
        d._manage(
            cfg_mod.FanSetting(channel="fan2", label="radiator", curve=[(30, 20)], source=source),
            FakeTarget("fan2", hub_fan_caps()),
        )
    assert len(d.managed) == 2


# --------------------------------------------------------------------- floors


def test_the_governor_gets_the_pumps_own_floor():
    d = make_daemon()
    d._manage(pump("cpu"), FakeTarget("pump", kraken_pump_caps()))
    d._manage(
        cfg_mod.FanSetting(channel="fan2", label="radiator", curve=[(30, 20)], source="cpu"),
        FakeTarget("fan2", hub_fan_caps()),
    )
    assert d.managed[0].governor.floor == caps_mod.PUMP_FLOOR
    assert d.managed[1].governor.floor == caps_mod.FAN_FLOOR


def test_shutdown_raises_the_fallback_to_each_channels_floor(monkeypatch):
    # One fallback_duty covers channels with different floors now that the pump
    # is managed here. Written raw, a quiet fallback would stop the pump.
    written: list[tuple[str, int]] = []
    monkeypatch.setattr(
        liquidctl_be, "set_fixed_speed", lambda dev, ch, duty: written.append((ch, duty))
    )

    d = make_daemon(fallback_duty=25)
    d._manage(pump("cpu"), FakeTarget("pump", kraken_pump_caps()))
    d._manage(
        cfg_mod.FanSetting(channel="fan2", label="radiator", curve=[(30, 20)], source="cpu"),
        FakeTarget("fan2", hub_fan_caps()),
    )
    d.shutdown()

    assert dict(written) == {"pump": caps_mod.PUMP_FLOOR, "fan2": 25}


def test_a_generous_fallback_is_left_alone(monkeypatch):
    written: list[tuple[str, int]] = []
    monkeypatch.setattr(
        liquidctl_be, "set_fixed_speed", lambda dev, ch, duty: written.append((ch, duty))
    )
    d = make_daemon(fallback_duty=60)
    d._manage(pump("cpu"), FakeTarget("pump", kraken_pump_caps()))
    d.shutdown()
    assert written == [("pump", 60)]


# ---------------------------------------------------------------- the curve


@pytest.mark.parametrize(
    ("temp", "expected"),
    [(41.1, 40), (83.4, 82), (84.8, 85), (95.0, 100), (120.0, 100)],
)
def test_the_measured_curve_interpolates_as_observed(temp, expected):
    # The values the running daemon logged, kept as a regression on the curve
    # actually in use.
    from fancontrol.curves import interpolate

    curve = [(50, 40), (70, 60), (85, 85), (95, 100)]
    assert interpolate(curve, temp) == expected
