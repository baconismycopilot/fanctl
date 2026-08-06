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
from fancontrol import devices as dev_mod
from fancontrol import targets as target_mod
from fancontrol.backends import liquidctl_be


@pytest.fixture
def writes(monkeypatch):
    """Record what reaches the device, keeping curves and duties apart."""
    log: dict[str, list] = {"fixed": [], "profile": []}
    monkeypatch.setattr(
        liquidctl_be, "set_fixed_speed", lambda dev, ch, duty: log["fixed"].append((ch, duty))
    )
    monkeypatch.setattr(
        liquidctl_be, "set_speed_profile", lambda dev, ch, prof: log["profile"].append((ch, prof))
    )
    return log


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


def test_shutdown_raises_the_fallback_to_each_channels_floor(writes):
    # One fallback_duty covers channels with different floors now that the pump
    # is managed here. Written raw, a quiet fallback would stop the pump.
    d = make_daemon(fallback_duty=25)
    d._manage(pump("cpu"), FakeTarget("pump", kraken_pump_caps()))
    d._manage(
        cfg_mod.FanSetting(channel="fan2", label="radiator", curve=[(30, 20)], source="cpu"),
        FakeTarget("fan2", hub_fan_caps()),
    )
    d.shutdown()

    assert writes["fixed"] == [("fan2", 25)]
    assert writes["profile"] == [("pump", daemon_mod.failsafe_curve(caps_mod.PUMP_FLOOR))]


def test_a_generous_fallback_is_left_alone(writes):
    d = make_daemon(fallback_duty=60)
    d._manage(pump("cpu"), FakeTarget("pump", kraken_pump_caps()))
    d.shutdown()
    assert writes["profile"] == [("pump", daemon_mod.failsafe_curve(60))]


# ------------------------------------------------------------- the failsafe


def test_a_managed_pump_duty_is_written_as_a_curve_not_a_duty(writes):
    # The Kraken holds either a fixed duty or a curve, never both, so a bare
    # duty would strand the pump on that number if this process is killed.
    d = make_daemon()
    d._manage(pump("cpu"), FakeTarget("pump", kraken_pump_caps()))
    d._write_duty(d.managed[0], 62)

    assert writes["fixed"] == []
    assert writes["profile"] == [("pump", daemon_mod.failsafe_curve(62))]


def test_a_hub_fan_still_gets_a_plain_duty(writes):
    # The hub has no device-side curve support; there is nothing to fall back to.
    d = make_daemon()
    d._manage(
        cfg_mod.FanSetting(channel="fan2", label="radiator", curve=[(30, 20)], source="liquid"),
        FakeTarget("fan2", hub_fan_caps()),
    )
    d._write_duty(d.managed[0], 47)

    assert writes["fixed"] == [("fan2", 47)]
    assert writes["profile"] == []


@pytest.mark.parametrize("duty", [30, 62, 100])
def test_the_cooler_runs_our_duty_everywhere_the_loop_actually_goes(duty):
    """What the device does with the curve, using liquidctl's own maths.

    The point of the failsafe curve is that it changes nothing while the daemon
    is alive: this loop spans 28.8-32.7C, so the flat section has to cover the
    whole band or the cooler would quietly override the CPU-keyed decision.
    """
    from liquidctl.util import interpolate_profile, normalize_profile

    curve = normalize_profile(daemon_mod.failsafe_curve(duty), 59)
    assert all(interpolate_profile(curve, liquid) == duty for liquid in range(20, 39))


def test_the_curve_still_ramps_if_the_loop_runs_away():
    from liquidctl.util import interpolate_profile, normalize_profile

    curve = normalize_profile(daemon_mod.failsafe_curve(40), 59)
    assert interpolate_profile(curve, 42) > 40
    assert interpolate_profile(curve, 45) == 100


# ------------------------------------------------------------------ wiring


def test_setup_wires_the_profile_pump_into_managed(monkeypatch):
    # Every other test drives _manage directly, so without this one the whole
    # 'if self.profile.pump' block could be deleted with the suite still green.
    caps = kraken_pump_caps()
    device = dev_mod.Device(
        alias="kraken", spec=dev_mod.REGISTRY[0x3012], serial="S", handle=object()
    )
    monkeypatch.setattr(dev_mod, "discover", lambda: [device])
    monkeypatch.setattr(caps_mod, "inspect_device", lambda d: caps)
    monkeypatch.setattr(target_mod, "inspect_device", lambda d: caps)

    d = daemon_mod.Daemon(
        config=cfg_mod.Config(), profile=cfg_mod.Profile(name="t", pump=pump("cpu"))
    )
    d.setup()

    assert [str(e.target) for e in d.managed] == ["kraken:pump"]


def test_a_tick_reads_every_source_before_it_writes_anything(writes, monkeypatch):
    # A status read chasing a write on the same hidraw node has returned an
    # impossible temperature. The pump put reads and writes on one device.
    order: list[str] = []
    monkeypatch.setattr(
        liquidctl_be, "set_fixed_speed", lambda dev, ch, duty: order.append(f"write:{ch}")
    )
    monkeypatch.setattr(
        liquidctl_be, "set_speed_profile", lambda dev, ch, prof: order.append(f"write:{ch}")
    )

    class Recording:
        def read(self, source):
            order.append(f"read:{source}")
            return 80.0

    d = make_daemon()
    d._manage(pump("cpu"), FakeTarget("pump", kraken_pump_caps()))
    d._manage(
        cfg_mod.FanSetting(channel="fan2", label="radiator", curve=[(30, 20)], source="liquid"),
        FakeTarget("fan2", hub_fan_caps()),
    )
    d.sources = Recording()
    d.tick()

    assert order == ["read:cpu", "read:liquid", "write:pump", "write:fan2"]


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
