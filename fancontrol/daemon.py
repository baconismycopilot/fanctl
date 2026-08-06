"""The curve daemon.

Exists because the Control Hub has no device-side curve support: it can hold a
fixed duty and nothing more. Anything temperature-reactive on those channels has
to be scheduled from userspace. The Kraken's pump curve, by contrast, runs on
the cooler itself and is written once at startup rather than every tick.

Three behaviours are worth knowing about:

**Nothing is held open.** Devices are opened per operation under the same
advisory lock the one-shot CLI uses. Holding handles across the daemon's life
would be faster, but it would make `fanctl status` block for as long as the
daemon runs. Coexisting with the CLI matters more than saving a connect.

**Suspend is detected, not hooked.** Sleep hooks are system-level and this runs
rootless as a user service, so instead the loop compares CLOCK_BOOTTIME against
CLOCK_MONOTONIC. Only the former advances across suspend, so a divergence means
the machine slept -- and since these devices keep no state through a power
cycle, everything is re-applied on wake.

**Exit raises the fans.** When the daemon stops, nothing is managing cooling any
more, and the hub will hold whatever duty was last written -- possibly an idle
25%. Leaving it there is a quiet trap for the next heavy workload, so shutdown
writes a safe fallback duty instead.
"""

from __future__ import annotations

import logging
import signal
import time
from dataclasses import dataclass, field

from . import caps as caps_mod
from . import config as cfg_mod
from . import curves as curve_mod
from . import devices as dev_mod
from . import render as render_mod
from . import targets as target_mod
from . import temps as temp_mod
from .backends import liquidctl_be

_LOGGER = logging.getLogger("fanctl.daemon")

# A boot-vs-monotonic divergence beyond this means the machine suspended, rather
# than the loop merely running late under load.
_SUSPEND_THRESHOLD = 10.0

# How often to rewrite duties regardless of whether the governor thinks they
# changed. The governor caches what it last applied, but a one-shot `fanctl fan
# set` writes straight to the device behind its back -- so without this a manual
# duty persists until the computed one happens to move, on a channel the daemon
# is supposed to own. Reasserting periodically keeps its model honest.
_REASSERT_TICKS = 60


@dataclass
class Managed:
    """One channel the daemon drives from a curve."""

    target: object
    governor: curve_mod.Governor
    source: str
    label: str


@dataclass
class Daemon:
    config: cfg_mod.Config
    profile: cfg_mod.Profile
    dry_run: bool = False

    devices: list = field(default_factory=list)
    managed: list[Managed] = field(default_factory=list)
    sources: temp_mod.Sources | None = None
    lcd_target: object = None
    _running: bool = True
    _last_frame: tuple | None = None
    _last_lcd_push: float = 0.0
    _pushed_frame: bool = False
    _ticks: int = 0

    # ------------------------------------------------------------------

    def setup(self) -> None:
        self.devices = dev_mod.discover()
        if not self.devices:
            raise RuntimeError("no supported NZXT devices found")

        liquid_device = None
        for device in self.devices:
            caps = caps_mod.inspect_device(device)
            if "pump" in caps.speed_channels:
                liquid_device = device
            if caps.screen and self.profile.lcd:
                self.lcd_target = device

        self.sources = temp_mod.Sources(
            device_reader=(lambda: _read_liquid(liquid_device)) if liquid_device else None
        )

        for fan in self.profile.fans:
            if fan.curve is None:
                continue
            target = target_mod.resolve(self.devices, fan.channel, "fan")
            if target.caps.speed_profile and fan.source == (
                target.caps.profile_temp_source or "liquid"
            ):
                # The device can run this itself; applied once, not scheduled.
                continue
            self.managed.append(
                Managed(
                    target=target,
                    governor=curve_mod.Governor(
                        curve=fan.curve,
                        floor=caps_mod.duty_floor("fan"),
                        min_step=self.config.daemon.min_step,
                    ),
                    source=fan.source,
                    label=fan.label,
                )
            )

        if self.lcd_target is not None and self.profile.lcd.mode == "stats" and not self.dry_run:
            # Buckets persist across processes and reboots; a stale set left by
            # earlier runs makes the first frame fail.
            try:
                liquidctl_be.reset_screen_buckets(self.lcd_target)
                _LOGGER.info("reclaimed LCD asset buckets")
            except Exception as exc:  # noqa: BLE001 - not worth failing startup
                _LOGGER.warning("could not reclaim LCD buckets: %s", exc)

        for key, value in self.sources.describe().items():
            _LOGGER.info("temperature source %-6s -> %s", key, value)
        for entry in self.managed:
            _LOGGER.info("managing %s from %s temperature", entry.target, entry.source)
        if not self.managed:
            _LOGGER.info("no software-scheduled curves in profile %r", self.profile.name)

    # ------------------------------------------------------------------

    def apply_static(self) -> None:
        """Write everything that is set once rather than scheduled."""
        import sys

        from .cli import apply_static_profile

        _, skipped = apply_static_profile(
            self.devices,
            self.profile,
            dry_run=self.dry_run,
            indent="  ",
            stats_handled=True,
        )
        # apply_static_profile prints; logging goes to stderr. Without a flush
        # the two interleave out of order whenever stdout is a pipe, which is
        # exactly how this runs under systemd.
        sys.stdout.flush()
        for note in skipped:
            _LOGGER.debug("not applied statically: %s", note)

    def tick(self, force: bool = False) -> None:
        self._ticks += 1
        if self._ticks % _REASSERT_TICKS == 0:
            _LOGGER.debug("reasserting duties on managed channels")
            force = True

        for entry in self.managed:
            temp = self.sources.read(entry.source)
            if temp is None:
                _LOGGER.warning("%s temperature unavailable; leaving %s alone", entry.source, entry.target)
                continue
            duty = entry.governor.update(temp, force=force)
            if duty is None:
                continue
            _LOGGER.info("%s %.1fC -> %d%%", entry.target, temp, duty)
            if not self.dry_run:
                liquidctl_be.set_fixed_speed(entry.target.device, entry.target.channel, duty)

        self._maybe_push_lcd(force=force)

    def _maybe_push_lcd(self, force: bool = False) -> None:
        lcd = self.profile.lcd
        if not lcd or lcd.mode != "stats" or self.lcd_target is None:
            return

        now = time.monotonic()
        if not force and (now - self._last_lcd_push) < self.config.daemon.lcd_interval:
            return

        readings = [(source, self.sources.read(source)) for source in lcd.sources]
        key = render_mod.frame_key(readings)
        if key == self._last_frame and not force:
            return  # nothing visibly changed; skip a 1.6 MB transfer

        self._last_lcd_push = now
        self._last_frame = key
        if self.dry_run:
            _LOGGER.info("would push LCD frame %s", key)
            return

        try:
            image = render_mod.render_stats(readings)
            liquidctl_be.set_screen_image(self.lcd_target, image)
            # First push at INFO so a working screen is visible in the log
            # without --verbose; after that DEBUG, since frames change often
            # enough that INFO would drown everything else.
            level = logging.DEBUG if self._pushed_frame else logging.INFO
            _LOGGER.log(level, "pushed LCD frame %s", key)
            self._pushed_frame = True
        except Exception as exc:  # noqa: BLE001 - never let the screen kill cooling
            _LOGGER.warning("LCD frame push failed: %s", exc)
            self._last_frame = None

    # ------------------------------------------------------------------

    def shutdown(self) -> None:
        """Leave the fans somewhere safe: nothing is managing them now."""
        duty = self.config.daemon.fallback_duty
        for entry in self.managed:
            if self.dry_run:
                _LOGGER.info("would set %s to fallback %d%%", entry.target, duty)
                continue
            try:
                liquidctl_be.set_fixed_speed(entry.target.device, entry.target.channel, duty)
                _LOGGER.info("%s -> fallback %d%%", entry.target, duty)
            except Exception as exc:  # noqa: BLE001 - best effort on the way out
                _LOGGER.error("could not set fallback on %s: %s", entry.target, exc)

    def run(self) -> int:
        self.setup()
        self.apply_static()

        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, self._stop)

        # Let the bus settle before the first read. apply_static has just issued
        # a burst of writes, and a status read chasing them has come back with a
        # physically impossible liquid temperature.
        time.sleep(0.5)

        interval = self.config.daemon.interval
        last_mono = time.monotonic()
        last_boot = time.clock_gettime(time.CLOCK_BOOTTIME)
        self.tick(force=True)

        while self._running:
            time.sleep(interval)
            if not self._running:
                break

            mono = time.monotonic()
            boot = time.clock_gettime(time.CLOCK_BOOTTIME)
            slept = boot - last_boot - (mono - last_mono)
            last_mono, last_boot = mono, boot

            resumed = slept > _SUSPEND_THRESHOLD
            if resumed:
                _LOGGER.info("resumed after ~%.0fs suspended; re-applying profile", slept)
                try:
                    self.apply_static()
                except Exception as exc:  # noqa: BLE001 - keep the loop alive
                    _LOGGER.error("re-apply after resume failed: %s", exc)

            try:
                self.tick(force=resumed)
            except Exception as exc:  # noqa: BLE001 - a bad poll must not stop cooling
                _LOGGER.error("tick failed: %s", exc)

        self.shutdown()
        return 0

    def _stop(self, *_args) -> None:
        _LOGGER.info("stopping")
        self._running = False


def _read_liquid(device) -> float | None:
    try:
        for reading in liquidctl_be.read_status(device):
            if "liquid" in reading.key.lower() and reading.unit == "°C":
                return float(reading.value)
    except Exception as exc:  # noqa: BLE001 - transient bus errors are expected
        _LOGGER.debug("liquid temperature read failed: %s", exc)
    return None
