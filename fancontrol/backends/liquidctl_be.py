"""liquidctl backend -- fans, pump, curves, LCD, and hub RGB.

liquidctl is imported as a library rather than driven as a subprocess. The CLI
costs a full device enumeration per invocation and reports failures as text on
stderr; importing gives us real exceptions and lets the daemon hold handles open
across polls.

The trade is that we depend on liquidctl's internal driver API, which carries no
stability guarantee. That dependency is deliberately confined to this module and
caps.py so a breaking upgrade has a small blast radius.
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass

from ..lock import device_lock

_LOGGER = logging.getLogger("fanctl.liquidctl")

# Upload attempts before giving up on a frame. A failed frame is not fatal --
# the daemon simply tries again next interval -- but a stale panel is visible.
_UPLOAD_ATTEMPTS = 3


@dataclass
class Reading:
    key: str
    value: object
    unit: str

    @property
    def is_empty_channel(self) -> bool:
        """Heuristic: a speed reading of exactly 0 rpm means nothing plugged in."""
        return self.unit == "rpm" and self.value == 0


@contextmanager
def opened(device):
    """Connect a device for the duration of a block, serialised by its lock."""
    with device_lock(device.serial):
        device.handle.connect()
        try:
            yield device.handle
        finally:
            try:
                device.handle.disconnect()
            except Exception:  # noqa: BLE001 - disconnect failures are not actionable
                pass


def read_status(device) -> list[Reading]:
    with opened(device) as handle:
        raw = handle.get_status()
    return [Reading(key=k, value=v, unit=u) for k, v, u in raw]


def pick_speed(readings: list[Reading], channel: str) -> float | None:
    """Find a channel's RPM in a status snapshot.

    liquidctl labels these 'Pump speed' and 'Fan 2 speed' while channels are
    named 'pump' and 'fan2', so both sides are squashed to a comparable form
    rather than maintaining a mapping table that would drift.
    """
    want = f"{channel}speed".lower().replace(" ", "")
    for reading in readings:
        if reading.unit != "rpm":
            continue
        if reading.key.lower().replace(" ", "") == want:
            return None if reading.value is None else float(reading.value)
    return None


def initialize(device) -> list[Reading]:
    """Initialise a device and report what it found.

    Required after a cold boot or a hardware change before LED accessories are
    addressable, and the only way to learn which accessory sits on which channel.
    """
    with opened(device) as handle:
        raw = handle.initialize()
    return [Reading(key=k, value=v, unit=u) for k, v, u in (raw or [])]


def set_fixed_speed(device, channel: str, duty: int) -> None:
    with opened(device) as handle:
        handle.set_fixed_speed(channel, duty)


def set_speed_profile(device, channel: str, profile: list[tuple[int, int]]) -> None:
    """Write a device-side duty curve. Only the Kraken supports this."""
    with opened(device) as handle:
        handle.set_speed_profile(channel, profile)


# Colour byte order: control_hub.py writes [g, r, b] with a `# GRB order`
# comment, which looks like a red/green transposition bug. It is not -- the
# hardware really is GRB, and requesting red on an F240 RGB Core produces red.
# Verified on hardware by capturing the wire bytes and checking the result; do
# not "fix" this by pre-swapping R and G, which transposes them for real.
def set_color(
    device,
    channel: str,
    mode: str,
    colors: list[list[int]],
    speed: str = "normal",
    direction: str = "forward",
) -> None:
    with opened(device) as handle:
        handle.set_color(channel, mode, colors, speed=speed, direction=direction)


def set_screen(device, mode: str, value: str | None) -> None:
    with opened(device) as handle:
        handle.set_screen("lcd", mode, value)


class ScreenUploadError(RuntimeError):
    """An LCD frame did not reach the panel."""


class _LiquidctlErrorCatcher(logging.Handler):
    """Captures the LCD failures liquidctl reports by logging rather than raising.

    `_setup_bucket` and `_switch_bucket` failing are both handled upstream with
    a bare `_LOGGER.error(...)` and no exception, so an upload that never
    reaches the panel returns exactly like one that did. Watching the log is the
    only signal available short of patching the driver.
    """

    def __init__(self) -> None:
        super().__init__(logging.ERROR)
        self.errors: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.errors.append(record.getMessage())


def reset_screen_buckets(device) -> None:
    """Reclaim all 16 LCD asset buckets.

    Each upload claims the next free bucket, and they persist across processes
    and reboots, so a long-running daemon eventually exhausts them and every
    subsequent frame fails. Clearing at startup gives a known-good slate.
    """
    with opened(device) as handle:
        handle._delete_all_buckets()


def set_screen_image(device, image) -> None:
    """Upload an in-memory PIL image to the LCD.

    liquidctl's screen API only takes a path, so the frame goes through a
    temporary file. The daemon renders several of these a minute, hence the
    unconditional cleanup.

    Bucket exhaustion is retried once after reclaiming, which is the documented
    failure mode for repeated uploads (liquidctl issue #774).
    """
    import os
    import tempfile

    handle_fd, path = tempfile.mkstemp(suffix=".png", prefix="fanctl-frame-")
    os.close(handle_fd)

    logger = logging.getLogger("liquidctl")
    catcher = _LiquidctlErrorCatcher()
    logger.addHandler(catcher)

    # Take liquidctl's errors off the console for the duration of the upload.
    # Bucket setup and switch fail often enough here (#774) that logging every
    # recovered attempt trains you to scroll past the line that matters. The
    # catcher still sees them, so nothing is lost: a recovered attempt is
    # reported at DEBUG below, and a frame that exhausts every attempt carries
    # the same messages out on ScreenUploadError for the caller to log.
    #
    # This is safe only because the whole block holds the device lock and the
    # daemon is single-threaded; nothing else in this process is talking to
    # liquidctl while propagation is off.
    propagate = logger.propagate
    logger.propagate = False
    try:
        image.save(path)
        with opened(device) as handle:
            # Every upload claims the next free bucket and never releases it, so
            # a daemon pushing frames fills all sixteen and then fails on each
            # one. Note what is occupied now: once the new frame is on screen,
            # all of these are stale. One 16-report query is cheap next to the
            # 1.6 MB transfer that follows.
            stale = {i for i, resp in handle._query_buckets().items() if any(resp[15:])}

            # Bucket setup and switch fail intermittently on this firmware even
            # with buckets free (upstream #774), and recover on a second try.
            # The pauses matter: retrying immediately tends to fail again.
            for attempt in range(_UPLOAD_ATTEMPTS):
                catcher.errors.clear()
                handle.set_screen("lcd", "static", path)
                if not catcher.errors:
                    break
                if attempt == _UPLOAD_ATTEMPTS - 1:
                    break
                _LOGGER.debug(
                    "LCD upload attempt %d failed (%s); reclaiming and retrying",
                    attempt + 1,
                    "; ".join(catcher.errors),
                )
                time.sleep(0.4)
                handle._delete_all_buckets()
                stale.clear()
                time.sleep(0.3)

            if not catcher.errors:
                # Safe now: the panel is displaying the newly written bucket.
                for index in stale:
                    handle._delete_bucket(index)

        if catcher.errors:
            raise ScreenUploadError("; ".join(catcher.errors))
    finally:
        logger.propagate = propagate
        logger.removeHandler(catcher)
        try:
            os.unlink(path)
        except OSError:
            pass
