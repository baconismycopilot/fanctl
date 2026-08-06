"""LCD uploads: a failure that liquidctl only logs must not read as success.

`_setup_bucket` and `_switch_bucket` fail by calling `_LOGGER.error` and
returning, so a frame that never reached the panel returns exactly like one
that did. Watching that log is the only signal available -- and the same
records must not reach the console for an attempt the retry recovered.
"""

from __future__ import annotations

import logging

import pytest
from PIL import Image

from fancontrol.backends import liquidctl_be

LIQUIDCTL = logging.getLogger("liquidctl")


class Handle:
    """Fails its first `fails` set_screen calls the way liquidctl does."""

    def __init__(self, fails: int):
        self.fails = fails
        self.calls = 0
        self.deleted: list = []

    def connect(self): ...
    def disconnect(self): ...
    def _query_buckets(self):
        return {0: [0] * 16, 1: [0] * 15 + [1]}

    def _delete_all_buckets(self):
        self.deleted.append("all")

    def _delete_bucket(self, index):
        self.deleted.append(index)

    def set_screen(self, *_args):
        self.calls += 1
        if self.calls <= self.fails:
            LIQUIDCTL.error("Failed to setup bucket for data transfer")
            LIQUIDCTL.error("Failed to switch active bucket")


class Device:
    alias = "stub"
    serial = "TEST-STUB-SERIAL"

    def __init__(self, handle):
        self.handle = handle


@pytest.fixture
def console():
    """Captures anything liquidctl manages to emit past the suppression."""

    class Capture(logging.Handler):
        def __init__(self):
            super().__init__(logging.ERROR)
            self.messages: list[str] = []

        def emit(self, record):
            self.messages.append(record.getMessage())

    capture = Capture()
    root = logging.getLogger()
    root.addHandler(capture)
    LIQUIDCTL.propagate = True
    try:
        yield capture
    finally:
        root.removeHandler(capture)


def upload(fails):
    handle = Handle(fails)
    liquidctl_be.set_screen_image(Device(handle), Image.new("RGB", (64, 64)))
    return handle


def test_recovered_attempt_is_silent(console):
    handle = upload(fails=1)
    assert handle.calls == 2
    assert console.messages == []
    assert LIQUIDCTL.propagate is True


def test_exhausted_upload_raises_and_carries_the_reason(console):
    with pytest.raises(liquidctl_be.ScreenUploadError) as excinfo:
        upload(fails=99)
    assert "Failed to setup bucket" in str(excinfo.value)
    # The caller logs this as one warning; the driver's own ERROR lines stay off
    # the console so a recovered retry cannot be mistaken for a real failure.
    assert console.messages == []
    assert LIQUIDCTL.propagate is True


def test_propagation_is_restored_when_the_driver_raises(console):
    class Boom(Handle):
        def set_screen(self, *_args):
            raise RuntimeError("bus error")

    with pytest.raises(RuntimeError):
        liquidctl_be.set_screen_image(Device(Boom(0)), Image.new("RGB", (64, 64)))
    # Leaving the logger muted would silence liquidctl for the daemon's life.
    assert LIQUIDCTL.propagate is True


def test_stale_buckets_are_pruned_after_a_good_push(console):
    # Buckets persist across processes and reboots; without this the sixteen
    # fill up and every later frame fails.
    handle = upload(fails=0)
    assert 1 in handle.deleted
