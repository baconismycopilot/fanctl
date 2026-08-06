"""Measure where a loop actually sits, and fit a curve to it.

A fan curve is only useful over the temperature range the hardware occupies.
Points below idle never engage, and points above the hottest the loop ever gets
are decoration -- a curve whose useful span is one segment wide has no
resolution where it matters. The range is not guessable: it depends on radiator
area, ambient, pump speed and how much heat the CPU actually dumps. It has to
be measured.

The measurement is less obvious than it looks. Load the machine, watch the
coolant settle, and you have measured *one point of a feedback loop* -- the
temperature is only that because the fans were running at whatever duty the
current curve chose for it. Change the curve and the plateau moves, so fitting
a new curve to that number is circular.

So the fans are pinned instead, and the loop is characterised at both ends:

- **Slowest** the fans are allowed to run: the hottest the loop gets on a
  workload that the curve is not helping with. This is where a curve should be
  demanding full airflow, because it is as bad as things get.
- **Full speed**: the coldest the loop can be held under the same load. Below
  this, extra airflow buys nothing -- there is no point putting curve points
  under it.

Between those two temperatures is the entire range in which a fan curve can
change any outcome. That is where the points go.
"""

from __future__ import annotations

import csv
import multiprocessing
import os
import subprocess
import time
from dataclasses import dataclass, field

from . import caps as caps_mod
from . import temps as temp_mod
from .backends import liquidctl_be

# The user service that would otherwise be writing to this channel while we are
# trying to hold it still.
UNIT = "fanctl"

# Sampling and settling. A loop with this much water in it moves slowly, so a
# plateau means "has not moved much in a while" rather than "stopped".
INTERVAL = 10.0
SETTLE_WINDOW = 150.0
SETTLE_DELTA = 0.3

# A load phase cannot be declared settled before this, however flat it looks.
# Coolant lags the die badly: on this hardware the CPU reaches 86C while the
# liquid has not moved at all, and that opening flat stretch is long enough to
# fill the settle window. Without a floor here the phase ends during the lag
# and reports the idle temperature as the loaded plateau.
MIN_LOAD_SECONDS = 300.0

# Refuse to keep heating past these. Tjmax on current desktop parts is around
# 95C and the chip throttles itself well before damage, but a calibration run
# is not the place to find out; the loop should never approach 45C at all.
CPU_LIMIT = 96.0
LIQUID_LIMIT = 45.0


class CalibrationError(RuntimeError):
    pass


@dataclass
class Sample:
    elapsed: float
    phase: str
    cpu: float | None
    liquid: float | None
    duty: int | None
    rpm: float | None


@dataclass
class Plateau:
    """Where the coolant settled with the fans held at one duty."""

    duty: int
    temp: float
    settled: bool  # False means it was still climbing when the phase ran out


@dataclass
class Result:
    idle: float
    plateaus: list[Plateau]
    peak: float
    samples: list[Sample] = field(default_factory=list)
    curve: list[tuple[int, int]] = field(default_factory=list)
    aborted: str | None = None

    @property
    def hot_slow(self) -> Plateau | None:
        """The worst case: fans held at their floor."""
        return min(self.plateaus, key=lambda p: p.duty) if self.plateaus else None

    @property
    def hot_fast(self) -> Plateau | None:
        """The best case: fans at full."""
        return max(self.plateaus, key=lambda p: p.duty) if self.plateaus else None

    @property
    def conclusive(self) -> bool:
        """Whether the phases support any claim about airflow.

        Selecting these by duty rather than temperature matters: a run whose
        phases never settled is a monotonic climb, so the *later* phase is
        hotter whatever its duty. Picking by temperature would label that
        inversion 'slow' and 'fast' and present it as a measurement of what
        airflow is worth, when it measures only the passage of time.
        """
        slow, fast = self.hot_slow, self.hot_fast
        if slow is None or fast is None or slow.duty == fast.duty:
            return False
        if not (slow.settled and fast.settled):
            return False
        return slow.temp > fast.temp


# ----------------------------------------------------------------------
# Load


def _spin(stop) -> None:
    # The event check is batched: testing it every iteration would spend a
    # meaningful share of the core on synchronisation rather than on heat.
    while not stop.is_set():
        for _ in range(500_000):
            pass


class Load:
    """Every core busy, for as long as the block runs."""

    def __init__(self, workers: int | None = None):
        self.workers = workers or os.cpu_count() or 4
        self._stop = multiprocessing.Event()
        self._procs: list = []

    def __enter__(self) -> Load:
        for _ in range(self.workers):
            proc = multiprocessing.Process(target=_spin, args=(self._stop,), daemon=True)
            proc.start()
            self._procs.append(proc)
        return self

    def __exit__(self, *_exc) -> None:
        self.stop()

    def stop(self) -> None:
        self._stop.set()
        for proc in self._procs:
            proc.join(timeout=5)
            if proc.is_alive():
                proc.terminate()
        self._procs = []


# ----------------------------------------------------------------------
# The daemon holds the channel we want to pin


def _systemctl(*args) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["systemctl", "--user", *args], capture_output=True, text=True, check=False
    )


def daemon_active() -> bool:
    return _systemctl("is-active", UNIT).stdout.strip() == "active"


# ----------------------------------------------------------------------
# Measurement


def _settled(samples: list[Sample], min_elapsed: float = 0.0) -> bool:
    """True once the coolant has stopped meaningfully climbing.

    The window is measured against the samples' own timestamps rather than a
    caller-supplied clock. Mixing the two is easy to do and hard to see: every
    phase after the first carries run-relative timestamps, so subtracting a
    phase-relative elapsed goes negative and quietly turns "the trailing 150
    seconds" into "everything so far", which then never settles.
    """
    timed = [(s.elapsed, s.liquid) for s in samples if s.liquid is not None]
    if not timed:
        return False

    if timed[-1][0] - timed[0][0] < min_elapsed:
        return False

    now = timed[-1][0]
    window = [value for stamp, value in timed if now - stamp <= SETTLE_WINDOW]
    # Enough of the window has to be filled for a flat stretch to mean anything;
    # two samples taken a moment apart are always flat.
    if len(window) < max(4, int(SETTLE_WINDOW / INTERVAL) - 1):
        return False
    return max(window) - min(window) < SETTLE_DELTA


def _read(sources, device, channel) -> tuple[float | None, float | None, int | None, float | None]:
    cpu = sources.read("cpu")
    liquid = sources.read("liquid")
    duty = rpm = None
    try:
        readings = liquidctl_be.read_status(device)
        rpm = liquidctl_be.pick_speed(readings, channel)
        want = f"{channel}duty".lower().replace(" ", "")
        for reading in readings:
            if reading.unit == "%" and reading.key.lower().replace(" ", "") == want:
                duty = int(reading.value)
    except Exception:
        pass
    return cpu, liquid, duty, rpm


def _phase(
    name: str,
    duty: int | None,
    limit: float,
    target,
    sources,
    result: Result,
    started: float,
    report,
    stop_early: bool,
) -> list[Sample]:
    """Sample until settled or out of time, holding `duty` if given."""
    samples: list[Sample] = []
    phase_start = time.monotonic()

    if duty is not None:
        liquidctl_be.set_fixed_speed(target.device, target.channel, duty)

    while True:
        now = time.monotonic()
        elapsed = now - phase_start
        if elapsed >= limit:
            return samples

        cpu, liquid, actual, rpm = _read(sources, target.device, target.channel)
        sample = Sample(
            elapsed=now - started, phase=name, cpu=cpu, liquid=liquid, duty=actual, rpm=rpm
        )
        samples.append(sample)
        result.samples.append(sample)

        if cpu is not None and cpu >= CPU_LIMIT:
            result.aborted = f"CPU reached {cpu:.1f}C"
            return samples
        if liquid is not None and liquid >= LIQUID_LIMIT:
            result.aborted = f"liquid reached {liquid:.1f}C"
            return samples

        report(sample)

        if stop_early and _settled(samples, MIN_LOAD_SECONDS):
            return samples

        # Re-pin every sample. The daemon is meant to be stopped for the
        # duration, but if it is running anyway this at least fights back
        # rather than silently measuring its duty instead of ours.
        if duty is not None and actual is not None and abs(actual - duty) > 1:
            liquidctl_be.set_fixed_speed(target.device, target.channel, duty)

        time.sleep(INTERVAL)


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    mid = len(ordered) // 2
    if not ordered:
        raise CalibrationError("no readings")
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def _spread(low: int, high: int, count: int) -> list[int]:
    """`count` strictly ascending integers from low to high."""
    if high - low < count - 1:
        high = low + count - 1
    out = [low]
    for i in range(1, count):
        value = round(low + (high - low) * i / (count - 1))
        out.append(max(value, out[-1] + 1))
    return out


def suggest_curve(
    idle: float, hot: float, min_duty: int, max_duty: int, points: int = 4
) -> list[tuple[int, int]]:
    """Fit a curve across the range the loop was measured to occupy.

    The bottom point sits at idle, so the fans are at their floor whenever
    nothing is happening. The top sits at the hottest the loop reached with the
    fans held slow -- the temperature at which airflow is most needed and least
    present. Everything in between is linear; a loop this heavily damped has no
    use for a shaped response, and a straight line is far easier to reason about
    when you come back to edit it.
    """
    low = int(idle)  # truncate, not round: the floor should start at or below idle
    high = max(int(hot + 0.999), low + 3)  # ceil, with room for a real ramp
    temps = _spread(low, high, points)
    duties = [round(min_duty + (max_duty - min_duty) * i / (points - 1)) for i in range(points)]
    return list(zip(temps, duties, strict=True))


def run(
    target,
    sources: temp_mod.Sources,
    *,
    idle_seconds: float = 120.0,
    phase_seconds: float = 900.0,
    cooldown_seconds: float = 300.0,
    min_duty: int | None = None,
    max_duty: int = 100,
    curve_max_duty: int = 100,
    report=lambda _s: None,
    note=lambda _m: None,
) -> Result:
    """Characterise the loop on `target`'s channel and fit a curve to it."""
    min_duty = min_duty if min_duty is not None else caps_mod.duty_floor("fan")
    started = time.monotonic()
    result = Result(idle=0.0, plateaus=[], peak=0.0)

    def phase(name, duty, limit, stop_early):
        return _phase(name, duty, limit, target, sources, result, started, report, stop_early)

    load = None
    try:
        note(f"idle baseline for {idle_seconds:.0f}s")
        idle_samples = phase("idle", min_duty, idle_seconds, stop_early=False)
        idle_values = [s.liquid for s in idle_samples if s.liquid is not None]
        if not idle_values:
            raise CalibrationError("no liquid temperature readings; is the cooler connected?")
        # Median over the tail: the start of this phase may still be shedding
        # heat from whatever the machine was doing beforehand.
        result.idle = _median(idle_values[len(idle_values) // 2 :])
        note(f"idle liquid {result.idle:.1f}C")

        load = Load()
        load.__enter__()

        for duty, label in ((min_duty, "slow"), (max_duty, "full")):
            if result.aborted:
                break
            note(f"load with fans held at {duty}% (up to {phase_seconds / 60:.0f} min)")
            samples = phase(f"load-{label}", duty, phase_seconds, stop_early=True)
            values = [s.liquid for s in samples if s.liquid is not None]
            if not values:
                continue
            tail = values[-3:]
            settled = _settled(samples)
            plateau = Plateau(duty=duty, temp=_median(tail), settled=settled)
            result.plateaus.append(plateau)
            note(
                f"fans {duty}%: liquid {plateau.temp:.1f}C"
                + ("" if settled else " (still climbing when the phase ended)")
            )

        load.stop()
        load = None

        if cooldown_seconds > 0 and not result.aborted:
            # The peak arrives after the load stops: the loop is still handing
            # over heat it absorbed minutes ago. That overshoot is real and a
            # curve fitted without it will be caught out by it.
            note(f"cooldown for {cooldown_seconds / 60:.0f} min, watching for the late peak")
            phase("cooldown", None, cooldown_seconds, stop_early=False)
    finally:
        if load is not None:
            load.stop()

    liquids = [s.liquid for s in result.samples if s.liquid is not None]
    result.peak = max(liquids) if liquids else result.idle

    # Only fit a curve to phases that actually settled. An unsettled run is a
    # snapshot of a climb, and a curve fitted to it would be presented with the
    # same confidence as a real measurement while being pure artefact.
    hot = result.hot_slow
    if hot is not None and result.conclusive:
        result.curve = suggest_curve(
            result.idle, max(hot.temp, result.peak), min_duty, curve_max_duty
        )
    return result


def write_csv(result: Result, path: str) -> None:
    with open(path, "w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["elapsed", "phase", "cpu", "liquid", "duty", "rpm"])
        for s in result.samples:
            writer.writerow(
                [
                    f"{s.elapsed:.0f}",
                    s.phase,
                    "" if s.cpu is None else f"{s.cpu:.1f}",
                    "" if s.liquid is None else f"{s.liquid:.1f}",
                    "" if s.duty is None else s.duty,
                    "" if s.rpm is None else f"{s.rpm:.0f}",
                ]
            )
