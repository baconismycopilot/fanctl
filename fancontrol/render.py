"""Render stat frames for the Kraken LCD.

The firmware has no CPU, GPU or fan-speed widget -- `liquid` is its only
built-in readout -- so showing anything else means drawing frames on the host
and uploading them. That is exactly what NZXT CAM does.

Constraints that shape the layout:

The Kraken Elite's panel is round, so everything stays inside the circle
inscribed in the 640x640 buffer. Corners are assumed lost even though a test
pattern suggested otherwise, because clipped digits are a worse failure than
slightly smaller ones. This is also why four readings use a 2x2 grid rather than
four stacked rows: the bottom row of a four-row stack sits where the circle has
narrowed to under half its width.

Text is drawn with a real TrueType face at a large size. PIL's default bitmap
font renders about six pixels tall here, which is illegible on a 2.7" panel --
and produced a memorable false alarm when a test image was mistaken for a
failed upload.
"""

from __future__ import annotations

from pathlib import Path

SIZE = 640
CENTRE = SIZE // 2

_FONT_CANDIDATES = [
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/TTF/LiberationSans-Bold.ttf",
]

BACKGROUND = (8, 8, 12)
LABEL = (130, 138, 150)
DIVIDER = (38, 40, 48)
NEUTRAL = (150, 205, 235)

CELSIUS = "°C"
RPM = "rpm"

# Temperature bands, warmest first. Colour carries the reading at a glance from
# across a room, where the digits themselves are not yet readable. Speeds get no
# bands: a high RPM is not a problem the way a high temperature is.
BANDS = [(80, (255, 64, 48)), (65, (255, 168, 32)), (0, (110, 220, 255))]


def _font(size: int):
    from PIL import ImageFont

    for candidate in _FONT_CANDIDATES:
        if Path(candidate).is_file():
            return ImageFont.truetype(candidate, size)
    return ImageFont.load_default()


def _colour_for(value: float | None, unit: str) -> tuple[int, int, int]:
    if value is None:
        return LABEL
    if unit != CELSIUS:
        return NEUTRAL
    for threshold, colour in BANDS:
        if value >= threshold:
            return colour
    return BANDS[-1][1]


def quantise(value: float | None, unit: str) -> int | None:
    """Round a reading to the precision actually shown.

    Fan and pump RPM jitter by a couple of counts between polls. Displaying
    that precision is meaningless, and treating it as a change would push a
    ~1.6 MB frame on almost every interval forever. Rounding to the nearest ten
    makes the frame stable when the hardware is.
    """
    if value is None:
        return None
    if unit == RPM:
        return int(round(value / 10.0) * 10)
    return int(round(value))


def _fmt(value: float | None, unit: str) -> str:
    quantised = quantise(value, unit)
    return "--" if quantised is None else str(quantised)


def _draw_reading(
    draw, cx: int, cy: int, reading, value_size: int, label_size: int, max_width: int
) -> None:
    """Draw one labelled reading centred on (cx, cy), shrunk to fit max_width."""
    label, value, unit = reading
    colour = _colour_for(value, unit)

    label_font = _font(label_size)
    superscript = unit == CELSIUS
    unit_text = "°" if superscript else unit.upper()

    # Shrink to fit. A two-digit temperature and a four-digit RPM differ in
    # width by more than a factor of two, so a size that suits one overflows
    # the other -- off the panel entirely in the 2x2 grid.
    text = _fmt(value, unit)
    while True:
        value_font = _font(value_size)
        unit_font = _font(round(value_size * (0.42 if superscript else 0.26)))
        gap = max(5, value_size // 20)
        value_w = draw.textbbox((0, 0), text, font=value_font, anchor="ls")[2]
        unit_w = draw.textbbox((0, 0), unit_text, font=unit_font, anchor="ls")[2]
        if value_w + gap + unit_w <= max_width or value_size <= 36:
            break
        value_size -= 4

    draw.text(
        (cx, cy - value_size // 2 - 4),
        label.upper(),
        font=label_font,
        fill=LABEL,
        anchor="mm",
    )

    # Centre the number and its unit as one group. Centring the digits alone and
    # hanging the unit off the right edge pushes the reading visibly off-axis,
    # which is obvious on a round panel.
    baseline = cy + value_size // 2 - value_size // 8
    start = cx - (value_w + gap + unit_w) / 2

    draw.text((start, baseline), text, font=value_font, fill=colour, anchor="ls")
    # A degree sign shares the digits' cap height; at this size difference it
    # would otherwise sit on the floor and read as a stray dot. A word-shaped
    # unit like RPM belongs on the baseline instead.
    unit_y = baseline - round(value_size * 0.45) if superscript else baseline
    draw.text((start + value_w + gap, unit_y), unit_text, font=unit_font, fill=colour, anchor="ls")


def render_stats(readings: list[tuple[str, float | None, str]]):
    """Draw up to four labelled readings onto a 640x640 frame.

    `readings` is [(label, value, unit), ...] in display order, where unit is
    '°C' or 'rpm'.
    """
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (SIZE, SIZE), BACKGROUND)
    draw = ImageDraw.Draw(image)

    readings = [_normalise(r) for r in readings[:4]]
    if not readings:
        return image

    if len(readings) == 4:
        # 2x2. Four stacked rows would push the outer two into the narrow part
        # of the circle; a grid keeps every cell well inside it.
        for index, reading in enumerate(readings):
            cx = CENTRE + (-142 if index % 2 == 0 else 142)
            cy = CENTRE + (-126 if index < 2 else 126)
            _draw_reading(draw, cx, cy, reading, 82, 26, max_width=248)
        draw.line([(CENTRE, CENTRE - 236), (CENTRE, CENTRE + 236)], fill=DIVIDER, width=3)
        draw.line([(CENTRE - 236, CENTRE), (CENTRE + 236, CENTRE)], fill=DIVIDER, width=3)
        return image

    if len(readings) == 1:
        rows = [CENTRE]
    elif len(readings) == 2:
        rows = [CENTRE - 118, CENTRE + 118]
    else:
        rows = [CENTRE - 168, CENTRE, CENTRE + 168]

    value_size = 150 if len(readings) <= 2 else 104
    label_size = 38 if len(readings) <= 2 else 28

    for index, (reading, y) in enumerate(zip(readings, rows)):
        if index:
            mid = (rows[index - 1] + y) // 2
            half = 150 if len(readings) <= 2 else 120
            draw.line([(CENTRE - half, mid), (CENTRE + half, mid)], fill=DIVIDER, width=3)
        _draw_reading(draw, CENTRE, y, reading, value_size, label_size, max_width=440)

    return image


def _normalise(reading) -> tuple[str, float | None, str]:
    """Accept (label, value) as a temperature, for older callers."""
    if len(reading) == 2:
        label, value = reading
        return label, value, CELSIUS
    return reading


# How far a reading must move to be worth redrawing. Quantising alone is not
# enough: a pump hovering either side of a rounding boundary flips the rounded
# value on every poll, so the threshold is measured against the last value
# actually displayed.
CHANGE_THRESHOLD = {CELSIUS: 1.0, RPM: 30.0}


def changed(previous, current) -> bool:
    """Whether a new sample is worth a fresh upload."""
    if previous is None or len(previous) != len(current):
        return True

    for old, new in zip(previous, current):
        old_label, old_value, old_unit = _normalise(old)
        new_label, new_value, new_unit = _normalise(new)
        if old_label != new_label or old_unit != new_unit:
            return True
        if (old_value is None) != (new_value is None):
            return True
        if new_value is None:
            continue
        if abs(new_value - old_value) >= CHANGE_THRESHOLD.get(new_unit, 1.0):
            return True

    return False


def frame_key(readings) -> tuple:
    """Identity of a frame's visible content.

    The daemon re-uploads only when this changes. Each upload is a ~1.6 MB bulk
    transfer that claims one of sixteen LCD buckets, so skipping redundant
    frames is a correctness measure and not just an optimisation.
    """
    out = []
    for reading in readings:
        label, value, unit = _normalise(reading)
        # Same quantisation the panel shows, so the key changes exactly when
        # the picture does.
        out.append((label, quantise(value, unit), unit))
    return tuple(out)
