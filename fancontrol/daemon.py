"""The curve daemon.

Exists because the Control Hub has no device-side curve support: it can hold a
fixed duty and nothing more. Anything temperature-reactive on those channels has
to be scheduled from userspace.

The Kraken's pump curve can run on the cooler itself, and is written once at
startup when it does. But the Kraken interpolates against its own liquid probe
and nothing else, so a pump curve keyed on CPU temperature is scheduled here
like any hub channel. That is worth doing: measurement on this loop put pump
speed at 2.6C of the CPU-to-coolant delta (2.1C of CPU temperature itself) and
roughly nothing on coolant temperature, so the thing a pump curve should react
to is the one the cooler cannot read.

Four behaviours are worth knowing about:

**Nothing is held open.** Devices are opened per operation under the same
advisory lock the one-shot CLI uses. Holding handles across the daemon's life
would be faster, but it would make `fanctl status` block for as long as the
daemon runs. Coexisting with the CLI matters more than saving a connect.

**Suspend is detected, not hooked.** Sleep hooks are system-level and this runs
rootless as a user service, so instead the loop compares CLOCK_BOOTTIME against
CLOCK_MONOTONIC. Only the former advances across suspend, so a divergence means
the machine slept -- and since these devices keep no state through a power
cycle, everything is re-initialised and re-applied on wake.

**Exit raises the fans.** When the daemon stops, nothing is managing cooling any
more, and the hub will hold whatever duty was last written -- possibly an idle
25%. Leaving it there is a quiet trap for the next heavy workload, so shutdown
writes a safe fallback duty instead.

**A managed duty is written as a curve wherever the device supports one.** The
Kraken is either holding a fixed duty or interpolating a curve, never both, so
driving the pump from here would otherwise strand it at a bare number the
moment this process dies -- and shutdown cannot help with a SIGKILL. Writing
each decision as the floor of a liquid curve keeps the cooler autonomous the
whole time: it holds our figure across the entire normal band, and still ramps
itself if the loop ever runs away. See `failsafe_curve`.
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
from . import gpu as gpu_mod
from . import memory as mem_mod
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

# Bounds of the emergency ramp on a device-side failsafe curve.
#
# The loop on this machine spans 28.8-32.7C, so the floor is held flat well past
# anything it reaches: inside the normal band the duty this daemon chose is the
# one that runs, and the curve is purely there to survive us. Past _NORMAL the
# only readings available are ones that mean the fans have failed or the pump has
# stalled, which is worth ramping hard for even though nothing is left to notice.
_NORMAL_LIQUID = 38
_EMERGENCY_LIQUID = 45

# Ticks to keep retrying OpenRGB-backed lighting that failed at startup. The
# unit orders us After= the OpenRGB server, but its SDK port opens before device
# detection finishes: OpenRGB 1.1 here took ~3s to list the Kraken, and a ring
# lookup in that window found nothing and was never tried again. At the default
# 2s interval this covers a minute, which outlasts detection comfortably.
_OPENRGB_RETRY_TICKS = 30


def failsafe_curve(duty: int) -> list[tuple[int, int]]:
    """A liquid curve that holds `duty` through the normal band, then ramps."""
    return [(20, duty), (_NORMAL_LIQUID, duty), (_EMERGENCY_LIQUID, 100)]


@dataclass
class Managed:
    """One channel the daemon drives from a curve."""

    target: target_mod.Target
    governor: curve_mod.Governor
    source: str
    label: str


@dataclass
class DisplaySpec:
    """One value on the LCD: either a temperature or a channel's RPM.

    `source` and `device`/`channel` are the two halves of that either: exactly
    one side is filled in, and which one is what `unit` reports.
    """

    label: str
    unit: str
    source: str | None = None  # temperature source, when unit is °C
    device: dev_mod.Device | None = None  # device to read, when unit is rpm
    channel: str | None = None


@dataclass
class Daemon:
    config: cfg_mod.Config
    profile: cfg_mod.Profile
    dry_run: bool = False

    devices: list[dev_mod.Device] = field(default_factory=list)
    managed: list[Managed] = field(default_factory=list)
    # Replaced wholesale in setup(); starting from an empty Sources rather than
    # None means every read path has one thing to deal with, not two.
    sources: temp_mod.Sources = field(default_factory=temp_mod.Sources)
    lcd_target: dev_mod.Device | None = None
    _running: bool = True
    _last_frame: list[tuple[str, float | None, str]] | None = None
    _last_lcd_push: float = 0.0
    _pushed_frame: bool = False
    _ticks: int = 0
    _display: list[DisplaySpec] = field(default_factory=list)
    _openrgb_retry: cfg_mod.Profile | None = None
    _openrgb_retries_left: int = 0

    # ------------------------------------------------------------------

    def _initialize_devices(self) -> None:
        """Put every device back into a state where it will report status.

        The Control Hub accepts a duty whether or not it has been initialised,
        but only begins streaming the 0x67 0x02 status reports a read consumes
        once `initialize` has asked it to. Skipping this leaves the daemon
        working perfectly while every read fails: `fanctl status` burns its
        twelve read attempts on the 0xff 0x03 chatter the hub sends regardless,
        and asserts `missing messages`. Nothing else on the system does this --
        not the unit, not the CLI unless invoked by hand -- so it belongs here,
        on the one path that runs at boot and again on wake.
        """
        if self.dry_run:
            return
        for device in self.devices:
            try:
                liquidctl_be.initialize(device)
                # Logged because it is otherwise invisible: the symptom of a
                # missing init shows up in a separate process reading status,
                # with nothing in the journal to connect it back to here.
                _LOGGER.info("initialised %s", device.alias)
            except Exception as exc:
                # One device that will not initialise is not a reason to leave
                # the rest of the machine uncooled.
                _LOGGER.error("could not initialise %s: %s", device.alias, exc)

    def setup(self) -> None:
        self.devices = dev_mod.discover()
        if not self.devices:
            raise RuntimeError("no supported NZXT devices found")

        # Before anything reads or writes: liquidctl's own contract is that no
        # method other than connect/disconnect is called until the device has
        # been initialised.
        self._initialize_devices()

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

        if self.profile.pump:
            self._manage(
                self.profile.pump,
                target_mod.sole_device_with(
                    self.devices,
                    lambda c: "pump" if "pump" in c.speed_channels else None,
                    "a pump channel",
                ),
            )

        for fan in self.profile.fans:
            self._manage(fan, target_mod.resolve(self.devices, fan.channel, "fan"))

        lcd = self.profile.lcd
        stats = self.lcd_target is not None and lcd is not None and lcd.mode == "stats"

        if stats and not self.dry_run:
            # Buckets persist across processes and reboots; a stale set left by
            # earlier runs makes the first frame fail.
            try:
                liquidctl_be.reset_screen_buckets(self.lcd_target)
                _LOGGER.info("reclaimed LCD asset buckets")
            except Exception as exc:
                _LOGGER.warning("could not reclaim LCD buckets: %s", exc)

        if stats:
            self._display = self._build_display_specs()

        for key, value in self.sources.describe().items():
            _LOGGER.info("temperature source %-6s -> %s", key, value)
        for entry in self.managed:
            _LOGGER.info("managing %s from %s temperature", entry.target, entry.source)
        if not self.managed:
            _LOGGER.info("no software-scheduled curves in profile %r", self.profile.name)

    def _build_display_specs(self) -> list[DisplaySpec]:
        """Resolve each configured LCD source to something readable."""
        specs: list[DisplaySpec] = []
        if self.profile.lcd is None:
            return specs

        for source in self.profile.lcd.sources:
            if source in temp_mod.PLAUSIBLE:
                specs.append(DisplaySpec(label=source, unit=render_mod.CELSIUS, source=source))
                continue

            # Anything else names a speed channel: 'pump', or a fan alias whose
            # RPM we read back from whichever device owns it. Aliases are a
            # config-level concept, so map through them before resolving.
            token = self.config.resolve_channel(source)
            try:
                target = target_mod.resolve(self.devices, token, "fan")
            except target_mod.TargetError as exc:
                _LOGGER.warning("LCD source %r skipped: %s", source, exc)
                continue

            specs.append(
                DisplaySpec(
                    label=source,
                    unit=render_mod.RPM,
                    device=target.device,
                    channel=target.channel,
                )
            )
            _LOGGER.info("LCD shows %s as %s rpm", source, target)

        return specs

    def _read_display(self) -> list[tuple[str, float | None, str]]:
        """Sample every LCD value, reading each device's status at most once."""
        cache: dict[str, list] = {}
        out: list[tuple[str, float | None, str]] = []

        for spec in self._display:
            # Branch on which half of the spec is filled in rather than on the
            # unit: they agree, but this is the one that says why.
            if spec.source is not None:
                out.append((spec.label, self.sources.read(spec.source), spec.unit))
                continue
            if spec.device is None or spec.channel is None:
                continue

            readings = cache.get(spec.device.serial)
            if readings is None:
                try:
                    readings = liquidctl_be.read_status(spec.device)
                except Exception as exc:
                    _LOGGER.debug("status read for %s failed: %s", spec.device.alias, exc)
                    readings = []
                cache[spec.device.serial] = readings

            out.append((spec.label, liquidctl_be.pick_speed(readings, spec.channel), spec.unit))

        return out

    # ------------------------------------------------------------------

    def apply_static(self) -> None:
        """Write everything that is set once rather than scheduled."""
        import sys

        from .cli import apply_static_profile

        failures, skipped = apply_static_profile(
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

        retry = self._openrgb_leds() if failures else []
        if retry:
            self._openrgb_retry = cfg_mod.Profile(name=self.profile.name, leds=retry)
            self._openrgb_retries_left = _OPENRGB_RETRY_TICKS
            _LOGGER.info("will retry %d OpenRGB lighting target(s) while it detects", len(retry))
        else:
            self._openrgb_retry = None

    def _openrgb_leds(self) -> list[cfg_mod.LedSetting]:
        """The profile's lighting that goes through OpenRGB rather than liquidctl."""
        out = []
        for led in self.profile.leds:
            if mem_mod.is_memory(led.channel) or gpu_mod.is_gpu(led.channel):
                out.append(led)
                continue
            try:
                target = target_mod.resolve(self.devices, led.channel, "led")
            except Exception:
                continue
            if target.caps.backend_for_color(target.channel) is caps_mod.Backend.OPENRGB:
                out.append(led)
        return out

    def _retry_openrgb(self) -> None:
        """Re-send OpenRGB lighting that failed at startup, until it lands or we give up."""
        import sys

        from .cli import apply_static_profile

        if self._openrgb_retry is None:
            return
        self._openrgb_retries_left -= 1
        failures, _ = apply_static_profile(
            self.devices, self._openrgb_retry, dry_run=self.dry_run, indent="  "
        )
        sys.stdout.flush()
        if not failures:
            _LOGGER.info("OpenRGB lighting applied on retry")
            self._openrgb_retry = None
        elif self._openrgb_retries_left <= 0:
            _LOGGER.error("OpenRGB lighting still failing; giving up until the next resume")
            self._openrgb_retry = None

    def _manage(self, setting, target) -> None:
        """Schedule a curve here, unless the device can run it itself.

        The pump goes through this on the same terms as a fan. Its curve runs
        on the cooler when it follows liquid temperature, but the Kraken can
        only interpolate against its own probe -- so a pump curve keyed on CPU
        has to be scheduled from here or it does not run at all.
        """
        if setting.curve is None:
            return
        if target.caps.speed_profile and setting.source == (
            target.caps.profile_temp_source or "liquid"
        ):
            return
        self.managed.append(
            Managed(
                target=target,
                governor=curve_mod.Governor(
                    curve=setting.curve,
                    floor=caps_mod.duty_floor(target.channel),
                    min_step=self.config.daemon.min_step,
                ),
                source=setting.source,
                label=setting.label,
            )
        )

    def tick(self, force: bool = False) -> None:
        self._ticks += 1
        if self._ticks % _REASSERT_TICKS == 0:
            _LOGGER.debug("reasserting duties on managed channels")
            force = True

        # Everything is read before anything is written. hidraw gives no
        # guarantee that a response belongs to the request you made, and a
        # status read chasing a write on the same device has come back with an
        # impossible liquid temperature. While only hub fans were managed the
        # order did not matter -- the writes went to a different device than the
        # reads. Managing the pump puts both on the Kraken.
        sampled = [(entry, self.sources.read(entry.source)) for entry in self.managed]
        self._maybe_push_lcd(force=force)

        for entry, temp in sampled:
            if temp is None:
                _LOGGER.warning(
                    "%s temperature unavailable; leaving %s alone", entry.source, entry.target
                )
                continue
            duty = entry.governor.update(temp, force=force)
            if duty is None:
                continue
            _LOGGER.info("%s %.1fC -> %d%%", entry.target, temp, duty)
            if not self.dry_run:
                self._write_duty(entry, duty)

    def _write_duty(self, entry: Managed, duty: int) -> None:
        """Push a decided duty to the device, as a curve where one is supported.

        A bare duty leaves the cooler with nothing to fall back on. Expressing
        it as the floor of a liquid curve costs one packet either way and means
        that whatever happens to this process -- SIGKILL, OOM, a crash between
        ticks -- the pump is still following a curve rather than frozen at the
        last number we happened to write.
        """
        target = entry.target
        if not target.caps.speed_profile:
            liquidctl_be.set_fixed_speed(target.device, target.channel, duty)
            return
        liquidctl_be.set_speed_profile(target.device, target.channel, failsafe_curve(duty))

    def _maybe_push_lcd(self, force: bool = False) -> None:
        lcd = self.profile.lcd
        if not lcd or lcd.mode != "stats" or self.lcd_target is None:
            return

        now = time.monotonic()
        if not force and (now - self._last_lcd_push) < self.config.daemon.lcd_interval:
            return

        readings = self._read_display()
        if not force and not render_mod.changed(self._last_frame, readings):
            return  # nothing visibly changed; skip a 1.6 MB transfer

        key = render_mod.frame_key(readings)
        self._last_lcd_push = now
        # Store the raw sample, not the rounded one: thresholds are measured
        # against what was last displayed, so comparing rounded values would
        # let a slow drift accumulate without ever tripping one.
        self._last_frame = readings
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
        except Exception as exc:
            _LOGGER.warning("LCD frame push failed: %s", exc)
            self._last_frame = None

    # ------------------------------------------------------------------

    def shutdown(self) -> None:
        """Leave the fans somewhere safe: nothing is managing them now."""
        for entry in self.managed:
            # One fallback_duty covers channels with different floors now that
            # the pump can be managed here, so it is raised per channel rather
            # than written raw -- a fallback of 25 must not stop the pump.
            duty = max(self.config.daemon.fallback_duty, caps_mod.duty_floor(entry.target.channel))
            if self.dry_run:
                _LOGGER.info("would set %s to fallback %d%%", entry.target, duty)
                continue
            try:
                self._write_duty(entry, duty)
                _LOGGER.info("%s -> fallback %d%%", entry.target, duty)
            except Exception as exc:
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
                    # A resumed device is in the same state as a cold-booted
                    # one: it has stopped reporting, and its LED accessories are
                    # not addressable until it has been asked to look for them
                    # again. Re-initialise before re-applying, not after.
                    self._initialize_devices()
                    self.apply_static()
                except Exception as exc:
                    _LOGGER.error("re-apply after resume failed: %s", exc)

            try:
                self.tick(force=resumed)
            except Exception as exc:
                _LOGGER.error("tick failed: %s", exc)

            try:
                self._retry_openrgb()
            except Exception as exc:
                _LOGGER.error("OpenRGB retry failed: %s", exc)

        self.shutdown()
        return 0

    def _stop(self, *_args) -> None:
        _LOGGER.info("stopping")
        self._running = False


def _read_liquid(device) -> float | None:
    try:
        for reading in liquidctl_be.read_status(device):
            if "liquid" in reading.key.lower() and reading.unit == "°C":
                return None if reading.value is None else float(reading.value)
    except Exception as exc:
        _LOGGER.debug("liquid temperature read failed: %s", exc)
    return None
