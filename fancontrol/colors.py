"""One colour and lighting-mode vocabulary across both backends.

liquidctl and OpenRGB name the same animations differently ("spectrum-wave" vs
"Spectrum Cycle") and support different subsets of them. Making the user
remember which tool owns which LED -- and then which spelling that tool wants --
is exactly the friction fanctl exists to remove, so the CLI speaks one canonical
vocabulary and each backend translates.

Which modes are actually *available* is derived per target at runtime, from the
driver's own mode table and from OpenRGB's reported mode list, rather than being
hardcoded here. This table only maps names.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModeSpec:
    """A canonical mode name and its spelling in each backend."""

    liquidctl: str | None
    openrgb: str | None


# Canonical name -> per-backend spelling. A None means that backend has no
# equivalent; availability is still checked per device at runtime.
MODES: dict[str, ModeSpec] = {
    "off": ModeSpec("off", None),  # ring has no Off mode; emulated as black
    "fixed": ModeSpec("fixed", "Static"),
    "fading": ModeSpec("fading", "Fading"),
    "spectrum-wave": ModeSpec("spectrum-wave", "Spectrum Cycle"),
    "covering-marquee": ModeSpec("covering-marquee", "Cover Marquee"),
    "super-rainbow": ModeSpec("super-rainbow", "Super Rainbow"),
    "marquee": ModeSpec(None, "Marquee"),
    "alternating": ModeSpec(None, "Alternating"),
    "pulsing": ModeSpec(None, "Pulsing"),
    "breathing": ModeSpec(None, "Breathing"),
    "candle": ModeSpec(None, "Candle"),
    "starry-night": ModeSpec(None, "Starry Night"),
    "rainbow-pulse": ModeSpec(None, "Rainbow Pulse"),
    "rainbow-flow": ModeSpec(None, "Rainbow Flow"),
    "direct": ModeSpec(None, "Direct"),
}

ANIMATION_SPEEDS = ["slowest", "slower", "normal", "faster", "fastest"]
DIRECTIONS = ["forward", "backward"]

BLACK = [0, 0, 0]


class ColorError(ValueError):
    pass


def parse_color(text: str) -> list[int]:
    """Parse a colour into [r, g, b].

    Delegates to liquidctl's parser so hex, rgb(), hsv() and hsl() all work
    identically whichever backend ends up serving the request.
    """
    from liquidctl.util import color_from_str

    try:
        return list(color_from_str(text))
    except Exception as exc:
        raise ColorError(
            f"cannot parse colour {text!r}; try ff0000, #ff0000, "
            "rgb(255,0,0), hsv(0,100,100) or hsl(0,100,50)"
        ) from exc


def canonical_for_liquidctl(name: str) -> str | None:
    spec = MODES.get(name)
    return spec.liquidctl if spec else None


def canonical_for_openrgb(name: str) -> str | None:
    spec = MODES.get(name)
    return spec.openrgb if spec else None
