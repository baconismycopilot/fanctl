"""Render stat frames for the Kraken LCD.

The firmware has no CPU or GPU widget -- `liquid` is its only built-in readout
-- so showing anything else means drawing frames on the host and uploading them.
That is exactly what NZXT CAM does.

Two constraints shape the layout:

The Kraken Elite's panel is round, so everything is kept inside the circle
inscribed in the 640x640 buffer. Corners are assumed lost even though a test
pattern suggested otherwise, because clipped digits are a worse failure than
slightly smaller ones.

Text is drawn with a real TrueType face at a large size. PIL's default bitmap
font renders about six pixels tall here, which is illegible on a 2.7" panel at
arm's length -- and produced a memorable false alarm when a test image was
mistaken for a failed upload.
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

# Temperature bands, warmest first. Colour carries the reading at a glance from
# across a room, where the digits themselves are not yet readable.
BANDS = [(80, (255, 64, 48)), (65, (255, 168, 32)), (0, (110, 220, 255))]


def _font(size: int):
    from PIL import ImageFont

    for candidate in _FONT_CANDIDATES:
        if Path(candidate).is_file():
            return ImageFont.truetype(candidate, size)
    return ImageFont.load_default()


def _colour_for(temp: float | None) -> tuple[int, int, int]:
    if temp is None:
        return LABEL
    for threshold, colour in BANDS:
        if temp >= threshold:
            return colour
    return BANDS[-1][1]


def _fmt(temp: float | None) -> str:
    return "--" if temp is None else f"{round(temp)}"


def render_stats(readings: list[tuple[str, float | None]]):
    """Draw up to three labelled temperatures onto a 640x640 frame.

    `readings` is [(label, celsius), ...] in display order.
    """
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (SIZE, SIZE), BACKGROUND)
    draw = ImageDraw.Draw(image)

    readings = readings[:3]
    if not readings:
        return image

    if len(readings) == 1:
        rows = [CENTRE]
    elif len(readings) == 2:
        rows = [CENTRE - 118, CENTRE + 118]
    else:
        rows = [CENTRE - 168, CENTRE, CENTRE + 168]

    value_size = 150 if len(readings) <= 2 else 104
    label_size = 38 if len(readings) <= 2 else 28
    unit_size = max(30, round(value_size * 0.42))
    value_font = _font(value_size)
    label_font = _font(label_size)
    unit_font = _font(unit_size)
    gap = max(6, value_size // 20)

    for index, ((label, temp), y) in enumerate(zip(readings, rows)):
        if index:
            # Divider sits midway between this row and the previous one.
            mid = (rows[index - 1] + y) // 2
            half = 150 if len(readings) <= 2 else 120
            draw.line([(CENTRE - half, mid), (CENTRE + half, mid)], fill=DIVIDER, width=3)

        colour = _colour_for(temp)
        draw.text(
            (CENTRE, y - value_size // 2 - 6),
            label.upper(),
            font=label_font,
            fill=LABEL,
            anchor="mm",
        )

        # Centre the number and its degree sign as one group. Drawing the digits
        # centred and then hanging the degree off the right edge pushes the
        # whole reading visibly off-axis, which is obvious on a round panel.
        text = _fmt(temp)
        value_box = draw.textbbox((0, 0), text, font=value_font, anchor="ls")
        unit_box = draw.textbbox((0, 0), "°", font=unit_font, anchor="ls")
        value_w = value_box[2] - value_box[0]
        unit_w = unit_box[2] - unit_box[0]

        baseline = y + value_size // 2 - value_size // 8
        start = CENTRE - (value_w + gap + unit_w) / 2

        draw.text((start, baseline), text, font=value_font, fill=colour, anchor="ls")
        # Share the digits' cap height rather than their baseline: at this size
        # difference the degree sign would otherwise sit on the floor, reading
        # as a stray dot instead of a unit.
        draw.text(
            (start + value_w + gap, baseline - round(value_size * 0.45)),
            "°",
            font=unit_font,
            fill=colour,
            anchor="ls",
        )

    return image


def frame_key(readings: list[tuple[str, float | None]]) -> tuple:
    """Identity of a frame's visible content.

    The daemon re-uploads only when this changes. Each upload is a ~1.6 MB bulk
    transfer that allocates and switches an LCD bucket, and upstream has an open
    report of rapid consecutive uploads failing to switch buckets, so skipping
    redundant frames is a correctness measure and not just an optimisation.
    """
    return tuple((label, None if t is None else round(t)) for label, t in readings)
