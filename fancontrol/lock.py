"""Cross-process locking for device I/O.

hidraw has no arbitration: two writers on one node produce dropped commands and
corrupt reads, with no error surfaced. Every touch of a device -- read or write,
CLI or daemon -- goes through here so that at least our own processes serialise.

This cannot protect against other tools (OpenRGB, CoolerControl, CAM). That is a
deployment concern, not something a lock file can fix.
"""

from __future__ import annotations

import fcntl
import os
from contextlib import contextmanager
from pathlib import Path

_LOCK_DIR = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "fanctl"


def _lock_path(key: str) -> Path:
    safe = key.replace("/", "_").strip("_")
    return _LOCK_DIR / f"{safe}.lock"


@contextmanager
def device_lock(key: str, timeout: float = 5.0):
    """Hold an exclusive advisory lock keyed on a device identity.

    `key` should be stable for the physical device -- the serial number, not the
    hidraw path, which changes across replug.
    """
    _LOCK_DIR.mkdir(parents=True, exist_ok=True)
    path = _lock_path(key)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = timeout
        delay = 0.05
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if deadline <= 0:
                    raise TimeoutError(
                        f"device {key} is busy; another fanctl process holds the lock"
                    ) from None
                # Deliberately a sleep and not a blocking flock: we want a
                # diagnosable timeout rather than a daemon wedged forever.
                import time

                time.sleep(delay)
                deadline -= delay
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
