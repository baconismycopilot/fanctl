"""Initialising the devices from the daemon.

The hardware fact behind this file: the Control Hub accepts a duty in any
state, but only starts streaming the status reports a read consumes after
`initialize` has asked it to. So a daemon that never initialises looks entirely
healthy -- fans spin, curves track -- while every status read on the machine
asserts `missing messages`, because the hub is sending nothing but its 0xff 0x03
chatter. The devices keep no state through a power cycle, so this has to happen
at startup and again on every resume.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import pytest

from fancontrol import caps as caps_mod
from fancontrol import config as cfg_mod
from fancontrol import daemon as daemon_mod
from fancontrol import devices as dev_mod
from fancontrol.backends import liquidctl_be


@dataclass
class FakeDevice:
    alias: str


@pytest.fixture
def initialised(monkeypatch):
    """Record which devices were asked to initialise, in order."""
    seen: list[str] = []
    monkeypatch.setattr(liquidctl_be, "initialize", lambda dev: seen.append(dev.alias) or [])
    return seen


def make_daemon(**kwargs) -> daemon_mod.Daemon:
    config = cfg_mod.Config(daemon=cfg_mod.DaemonSettings())
    return daemon_mod.Daemon(config=config, profile=cfg_mod.Profile(name="t"), **kwargs)


def with_devices(d: daemon_mod.Daemon, *aliases: str) -> daemon_mod.Daemon:
    d.devices = [FakeDevice(alias) for alias in aliases]
    return d


# ------------------------------------------------------------------ at startup


def test_setup_initialises_every_discovered_device(monkeypatch, initialised):
    devs = [FakeDevice("hub"), FakeDevice("kraken")]
    monkeypatch.setattr(dev_mod, "discover", lambda: devs)
    monkeypatch.setattr(
        caps_mod, "inspect_device", lambda dev: caps_mod.Capabilities(speed_channels=[])
    )

    make_daemon().setup()

    assert initialised == ["hub", "kraken"]


def test_a_device_that_will_not_initialise_does_not_stop_the_others(monkeypatch):
    # One dead device is not a reason to leave the rest of the machine uncooled,
    # so this logs and carries on rather than raising out of setup.
    seen: list[str] = []

    def flaky(dev):
        if dev.alias == "hub":
            raise OSError("read error")
        seen.append(dev.alias)
        return []

    monkeypatch.setattr(liquidctl_be, "initialize", flaky)
    with_devices(make_daemon(), "hub", "kraken")._initialize_devices()

    assert seen == ["kraken"]


def test_dry_run_writes_nothing(initialised):
    with_devices(make_daemon(dry_run=True), "hub", "kraken")._initialize_devices()
    assert initialised == []


# -------------------------------------------------------------------- on wake


def test_resume_reinitialises_before_reapplying(monkeypatch, initialised):
    """A resumed device is in the same state as a cold-booted one.

    Order matters: re-applying a profile to a device that has not been asked to
    look for its accessories again writes to channels it no longer knows about.
    """
    order: list[str] = []
    monkeypatch.setattr(liquidctl_be, "initialize", lambda dev: order.append("init") or [])

    d = with_devices(make_daemon(), "hub")
    monkeypatch.setattr(d, "setup", lambda: None)
    monkeypatch.setattr(d, "apply_static", lambda: order.append("apply"))
    monkeypatch.setattr(d, "shutdown", lambda: None)

    # Second tick ends the loop; the first is the pre-loop one in run().
    ticks = iter([None, "stop"])
    monkeypatch.setattr(d, "tick", lambda force=False: setattr(d, "_running", next(ticks) is None))

    monkeypatch.setattr(time, "sleep", lambda _secs: None)
    # Only CLOCK_BOOTTIME advances across suspend; a jump against a monotonic
    # clock that barely moved is exactly how the loop detects the machine slept.
    boots = iter([0.0, 3600.0])
    monkeypatch.setattr(time, "clock_gettime", lambda _clk: next(boots))

    d.run()

    assert order == ["apply", "init", "apply"]
