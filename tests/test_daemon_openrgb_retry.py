"""Retrying OpenRGB lighting that the daemon could not apply at startup.

The hardware fact: OpenRGB's SDK port accepts connections before device
detection has finished. The daemon starts right after the server, so its first
ring lookup came back empty ("OpenRGB does not expose a ring for kraken") and
the ring sat on its firmware default until someone ran `fanctl apply`. Only the
OpenRGB-backed targets are retried; liquidctl lighting either worked the first
time or will not be fixed by waiting.
"""

from __future__ import annotations

import pytest

from fancontrol import cli as cli_mod
from fancontrol import config as cfg_mod
from fancontrol import daemon as daemon_mod

RING = cfg_mod.LedSetting(channel="ring", label="ring", mode="fixed", colors=["00ccff"])
HUB = cfg_mod.LedSetting(channel="hub:2", label="f240", mode="fixed", colors=["00ccff"])


def make_daemon() -> daemon_mod.Daemon:
    profile = cfg_mod.Profile(name="t", leds=[RING, HUB])
    return daemon_mod.Daemon(config=cfg_mod.Config(), profile=profile)


@pytest.fixture
def applies(monkeypatch):
    """Script apply_static_profile's failure counts and record what each call carried."""
    calls: list[list[str]] = []
    results: list[int] = []

    def fake(devices, profile, dry_run=False, force=False, indent="  ", stats_handled=False):
        calls.append([led.channel for led in profile.leds])
        return (results.pop(0) if results else 0), []

    monkeypatch.setattr(cli_mod, "apply_static_profile", fake)
    monkeypatch.setattr(daemon_mod.Daemon, "_openrgb_leds", lambda self: [RING])
    return calls, results


def test_retries_only_openrgb_targets_until_they_land(applies):
    calls, results = applies
    results += [1, 1, 0]  # startup fails, one retry fails, the next lands
    d = make_daemon()

    d.apply_static()
    d._retry_openrgb()
    d._retry_openrgb()
    d._retry_openrgb()  # nothing pending any more: must not call again

    assert calls == [["ring", "hub:2"], ["ring"], ["ring"]]
    assert d._openrgb_retry is None


def test_gives_up_after_the_retry_budget(applies, monkeypatch):
    calls, results = applies
    monkeypatch.setattr(daemon_mod, "_OPENRGB_RETRY_TICKS", 2)
    results += [1, 1, 1, 1]
    d = make_daemon()

    d.apply_static()
    for _ in range(4):
        d._retry_openrgb()

    assert len(calls) == 3  # startup plus exactly two retries
    assert d._openrgb_retry is None


def test_clean_startup_schedules_nothing(applies):
    calls, _ = applies
    d = make_daemon()

    d.apply_static()
    d._retry_openrgb()

    assert len(calls) == 1
