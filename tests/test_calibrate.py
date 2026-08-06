"""Calibration logic: curve fitting and plateau detection.

Both plateau bugs these cover were found by running the command against real
hardware rather than fixtures, and both needed a phase that does not start at
t=0 to reproduce -- so the offsets here are not incidental.
"""

from __future__ import annotations

from itertools import pairwise

import pytest

from fancontrol import calibrate as cal
from fancontrol.cli import build_parser
from fancontrol.config import _parse_curve
from fancontrol.curves import interpolate

STEP = cal.INTERVAL


def series(values, start=0.0):
    """Samples carrying run-relative timestamps, as a real phase produces."""
    return [
        cal.Sample(elapsed=start + i * STEP, phase="p", cpu=86.0, liquid=v, duty=20, rpm=490)
        for i, v in enumerate(values)
    ]


def result(plateaus):
    return cal.Result(idle=30.0, plateaus=plateaus, peak=33.0)


# ----------------------------------------------------------------- spread


@pytest.mark.parametrize(
    ("low", "high", "expected"),
    [
        (30, 35, [30, 32, 33, 35]),
        (30, 31, [30, 31, 32, 33]),  # too narrow: widened, never collapsed
        (30, 30, [30, 31, 32, 33]),
    ],
)
def test_spread(low, high, expected):
    assert cal._spread(low, high, 4) == expected


@pytest.mark.parametrize(("low", "high"), [(28, 29), (30, 45), (20, 22), (35, 36)])
def test_spread_is_strictly_ascending(low, high):
    # config rejects a curve whose temperatures are not ascending, and
    # interpolate walks consecutive pairs, so this is a hard requirement.
    points = cal._spread(low, high, 4)
    assert all(b > a for a, b in pairwise(points))


# ------------------------------------------------------------ curve fitting


def test_suggested_curve_spans_the_measured_range():
    curve = cal.suggest_curve(idle=30.2, hot=34.5, min_duty=20, max_duty=100)
    assert curve[0] == (30, 20)
    assert curve[-1][1] == 100
    # The floor must not sit above idle, or the fans ramp while the machine is
    # doing nothing at all.
    assert curve[0][0] <= 30.2


def test_suggested_curve_survives_a_loop_with_almost_no_spread():
    curve = cal.suggest_curve(idle=31.0, hot=31.4, min_duty=20, max_duty=100)
    assert len(curve) == 4
    assert curve[-1][1] == 100
    assert all(b > a for (a, _), (b, _) in pairwise(curve))


def test_suggested_curve_is_accepted_downstream():
    curve = cal.suggest_curve(idle=30.2, hot=34.5, min_duty=20, max_duty=100)
    assert _parse_curve([list(p) for p in curve], "test") == curve
    for temp in (25.0, 30.0, 32.5, 34.9, 50.0):
        assert 20 <= interpolate(curve, temp) <= 100
    assert interpolate(curve, 10.0) == 20
    assert interpolate(curve, 99.0) == 100


# -------------------------------------------------------- plateau detection


def test_flat_opening_of_a_load_phase_is_not_a_plateau():
    # Measured: the CPU reaches 86C while the coolant has not moved at all, and
    # that lag outlasts the settle window. Without the minimum the phase ends
    # during it and reports the idle temperature as the loaded plateau.
    lag = [29.6] * 20 + [29.8, 30.2, 30.9, 31.7]
    assert not cal._settled(series(lag), cal.MIN_LOAD_SECONDS)
    assert cal._settled(series([29.6] * 20)), "sanity: flat is flat without the floor"


def test_detection_does_not_depend_on_when_the_phase_started():
    # The old code subtracted a phase-relative elapsed from a run-relative
    # stamp, so every phase after the first behaved differently.
    values = [30.0 + min(i, 12) * 0.3 for i in range(16)] + [33.6] * 18
    assert cal._settled(series(values, start=0.0))
    assert cal._settled(series(values, start=1200.0))


@pytest.mark.parametrize("start", [0.0, 1200.0])
def test_climbing_is_never_settled(start):
    assert not cal._settled(series([30.0 + i * 0.4 for i in range(24)], start=start))


def test_window_is_trailing():
    # Early values outside the delta must not stop a settled tail registering.
    assert cal._settled(series([30.0, 31.0, 32.0, 33.0] + [33.5] * 20, start=900.0))


def test_settled_rejects_degenerate_input():
    assert not cal._settled([])
    assert not cal._settled(series([33.5, 33.5]))
    blank = [
        cal.Sample(elapsed=i * STEP, phase="p", cpu=86.0, liquid=None, duty=20, rpm=0)
        for i in range(20)
    ]
    assert not cal._settled(blank)


def test_against_the_recorded_soak():
    # The real 15-minute run plateaued at 33.5C. It must be found at the end
    # and not at the six-minute mark, where it was still climbing.
    soak = [30.2, 30.7, 31.7, 32.2, 32.7, 33.0, 33.3, 33.4, 33.5, 33.5, 33.5, 33.5, 33.5]
    full = series([v for v in soak for _ in range(6)])
    assert cal._settled(full, cal.MIN_LOAD_SECONDS)
    assert not cal._settled(full[: len(full) // 2], cal.MIN_LOAD_SECONDS)


# ------------------------------------------------------------- conclusions


def test_plateaus_are_keyed_on_duty_not_temperature():
    # An unsettled run is a monotonic climb, so the later phase is hotter
    # whatever duty it ran at. Keying on temperature labelled that inversion
    # 'slow' and 'fast' and reported it as what airflow is worth.
    run = result([cal.Plateau(20, 30.3, False), cal.Plateau(100, 31.0, False)])
    assert run.hot_slow.duty == 20
    assert run.hot_fast.duty == 100
    assert not run.conclusive


def test_conclusive_requires_settled_sane_phases():
    assert result([cal.Plateau(20, 35.0, True), cal.Plateau(100, 32.0, True)]).conclusive
    # Slow fans colder than fast ones is nonsense; refuse to draw from it.
    assert not result([cal.Plateau(20, 31.0, True), cal.Plateau(100, 31.5, True)]).conclusive
    assert not result([cal.Plateau(20, 35.0, True)]).conclusive
    assert not result([]).conclusive


# -------------------------------------------------------------- cli wiring


def test_calibrate_parses():
    args = build_parser().parse_args(["calibrate", "radiator", "--phase", "60", "--csv", "x.csv"])
    assert args.channel == "radiator"
    assert args.phase == 60.0
    assert args.manage_daemon is True

    args = build_parser().parse_args(["calibrate", "hub:2", "--no-manage-daemon"])
    assert args.manage_daemon is False
    assert args.curve_max_duty == 100
