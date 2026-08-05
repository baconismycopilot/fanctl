"""TOML profiles.

A profile is a named, whole-machine state: fan duties, a pump curve, lighting on
both backends, and what the LCD shows. `fanctl apply quiet` puts every device
into that state in one go.

Two things shape the schema:

Channel aliases exist because `hub:2` says nothing about what it cools, and the
hardware cannot tell you -- whether a fan is bolted to a radiator or a case
panel is knowledge that lives outside the device. Naming it `radiator` once, in
config, keeps that knowledge in one place and out of every command.

Fan settings accept either a fixed duty or a curve, even though nothing here can
run a curve on the Control Hub yet. The hub has no device-side curve support, so
those need the daemon. Storing them now means phase 4 adds an executor rather
than a schema migration, and `apply` reports what it had to skip rather than
silently doing less than the config asks for.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

TEMP_SOURCES = ("liquid", "cpu", "gpu")


class ConfigError(ValueError):
    pass


def config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "fanctl" / "config.toml"


@dataclass
class FanSetting:
    channel: str  # target token, e.g. 'hub:2' or 'pump'
    label: str  # friendly name as written in config
    duty: int | None = None
    curve: list[tuple[int, int]] | None = None
    source: str = "liquid"

    @property
    def needs_daemon_on(self) -> bool:
        """A curve needs software scheduling unless the device runs it itself."""
        return self.curve is not None


@dataclass
class LedSetting:
    channel: str
    label: str
    mode: str
    colors: list[str] = field(default_factory=list)
    speed: str = "normal"
    direction: str = "forward"


@dataclass
class LcdSetting:
    mode: str | None = None  # liquid | image | stats
    path: str | None = None
    brightness: int | None = None
    orientation: int | None = None


@dataclass
class Profile:
    name: str
    description: str = ""
    fans: list[FanSetting] = field(default_factory=list)
    pump: FanSetting | None = None
    leds: list[LedSetting] = field(default_factory=list)
    lcd: LcdSetting | None = None


@dataclass
class Config:
    channels: dict[str, str] = field(default_factory=dict)
    profiles: dict[str, Profile] = field(default_factory=dict)
    path: Path | None = None

    def resolve_channel(self, name: str) -> str:
        """Map a friendly name to a target token, or pass a token through."""
        return self.channels.get(name, name)


def _parse_curve(raw, where: str) -> list[tuple[int, int]]:
    if not isinstance(raw, list) or len(raw) < 2:
        raise ConfigError(f"{where}: a curve needs at least two [temp, duty] points")
    points: list[tuple[int, int]] = []
    for item in raw:
        if not isinstance(item, list) or len(item) != 2:
            raise ConfigError(f"{where}: each curve point must be [temp, duty], got {item!r}")
        try:
            points.append((int(item[0]), int(item[1])))
        except (TypeError, ValueError):
            raise ConfigError(f"{where}: curve point {item!r} must be two integers") from None

    temps = [t for t, _ in points]
    if temps != sorted(temps) or len(set(temps)) != len(temps):
        raise ConfigError(f"{where}: curve temperatures must be strictly ascending")
    return points


def _parse_fan(label: str, raw, config: Config, where: str) -> FanSetting:
    channel = config.resolve_channel(label)

    if isinstance(raw, int):
        return FanSetting(channel=channel, label=label, duty=raw)

    if not isinstance(raw, dict):
        raise ConfigError(f"{where}.{label}: expected a duty number or a table, got {raw!r}")

    if "duty" in raw and "curve" in raw:
        raise ConfigError(f"{where}.{label}: set either 'duty' or 'curve', not both")

    if "duty" in raw:
        return FanSetting(channel=channel, label=label, duty=int(raw["duty"]))

    if "curve" in raw:
        source = str(raw.get("source", "liquid")).lower()
        if source not in TEMP_SOURCES:
            raise ConfigError(
                f"{where}.{label}: unknown source {source!r}; "
                f"expected one of {', '.join(TEMP_SOURCES)}"
            )
        return FanSetting(
            channel=channel,
            label=label,
            curve=_parse_curve(raw["curve"], f"{where}.{label}"),
            source=source,
        )

    raise ConfigError(f"{where}.{label}: needs a 'duty' or a 'curve'")


def _parse_led(label: str, raw, config: Config, where: str) -> LedSetting:
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}.{label}: expected a table with at least a 'mode'")
    if "mode" not in raw:
        raise ConfigError(f"{where}.{label}: missing 'mode'")

    colors = raw.get("colors", [])
    if isinstance(colors, str):
        colors = [colors]
    if not isinstance(colors, list):
        raise ConfigError(f"{where}.{label}: 'colors' must be a list of colour strings")

    return LedSetting(
        channel=config.resolve_channel(label),
        label=label,
        mode=str(raw["mode"]),
        colors=[str(c) for c in colors],
        speed=str(raw.get("speed", "normal")),
        direction=str(raw.get("direction", "forward")),
    )


def _parse_lcd(raw, where: str) -> LcdSetting:
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}: expected a table")
    setting = LcdSetting(
        mode=str(raw["mode"]) if "mode" in raw else None,
        path=str(raw["path"]) if "path" in raw else None,
        brightness=int(raw["brightness"]) if "brightness" in raw else None,
        orientation=int(raw["orientation"]) if "orientation" in raw else None,
    )
    if setting.mode == "image" and not setting.path:
        raise ConfigError(f"{where}: mode 'image' needs a 'path'")
    return setting


def _parse_profile(name: str, raw, config: Config) -> Profile:
    if not isinstance(raw, dict):
        raise ConfigError(f"profiles.{name}: expected a table")

    where = f"profiles.{name}"
    profile = Profile(name=name, description=str(raw.get("description", "")))

    for label, value in (raw.get("fans") or {}).items():
        profile.fans.append(_parse_fan(label, value, config, f"{where}.fans"))

    if "pump" in raw:
        profile.pump = _parse_fan("pump", raw["pump"], config, where)

    for label, value in (raw.get("led") or {}).items():
        profile.leds.append(_parse_led(label, value, config, f"{where}.led"))

    if "lcd" in raw:
        profile.lcd = _parse_lcd(raw["lcd"], f"{where}.lcd")

    return profile


_PRESETS = [
    ("quiet", "near-silent at idle", 25, [(30, 40), (40, 60), (50, 100)], "0044ff"),
    ("balanced", "everyday default", 40, [(30, 50), (40, 75), (50, 100)], "00ccff"),
    ("performance", "maximum cooling", 80, [(30, 70), (40, 90), (50, 100)], "ff2200"),
]


def generate_starter(channels: list[dict], has_pump: bool, has_lcd: bool) -> str:
    """Render a starter config from what was actually discovered.

    Each entry in `channels` is {label, target, fan: bool, led: bool}. Targets
    use the numeric shorthand ('hub:2') deliberately: it resolves to fan2 in a
    fans block and led2 in a led block, so one alias names the physical thing
    once and serves both.

    Only populated channels are emitted. An empty fan header is noise in a
    config file, and a duty written to one does nothing.
    """
    lines = [
        "# fanctl configuration",
        "#",
        "# Channel aliases give physical meaning to hardware channels. The device",
        "# cannot tell you what a fan is bolted to, so record it here once and",
        "# refer to it by name everywhere else. Rename these to match your build",
        "# -- 'radiator' and 'intake' beat 'hub_2' when you come back in a month.",
        "",
        "[channels]",
    ]
    for ch in channels:
        lines.append(f'{ch["label"]} = "{ch["target"]}"')
    lines.append("")

    fan_labels = [c["label"] for c in channels if c.get("fan")]
    led_labels = [c["label"] for c in channels if c.get("led")]

    for name, description, duty, pump_curve, colour in _PRESETS:
        lines += [f"[profiles.{name}]", f'description = "{description}"', ""]

        if fan_labels:
            lines.append(f"[profiles.{name}.fans]")
            lines += [f"{label} = {duty}" for label in fan_labels]
            lines.append("")

        if has_pump:
            points = ", ".join(f"[{t}, {d}]" for t, d in pump_curve)
            lines += [
                f"[profiles.{name}.pump]",
                f"curve = [{points}]",
                "",
            ]

        if led_labels:
            lines.append(f"[profiles.{name}.led]")
            lines += [
                f'{label} = {{ mode = "fixed", colors = ["{colour}"] }}' for label in led_labels
            ]
            lines.append("")

        if has_lcd:
            lines += [f"[profiles.{name}.lcd]", 'mode = "liquid"', ""]

    lines += [
        "# Notes",
        "#",
        "# Lighting mode names are canonical across both backends. Run",
        "# 'fanctl caps' to see which modes a given target supports -- the hub",
        "# implements six, the pump ring fifteen.",
        "#",
        "# The LCD firmware has no CPU or GPU widget, so 'liquid' is the only",
        "# built-in readout. Showing anything else means rendering frames on the",
        "# host, which arrives with the daemon as mode = \"stats\".",
        "#",
        "# A curve on a Control Hub channel needs the daemon: the hub has no",
        "# device-side curve support, and 'apply' will say so rather than",
        "# silently doing less than you asked. The pump curves above run on the",
        "# cooler itself and work today. To use one, replace a fan's duty with:",
        "#",
        '#   radiator = { curve = [[30, 30], [35, 50], [40, 80]], source = "liquid" }',
    ]

    return "\n".join(lines) + "\n"


def load(path: Path | None = None) -> Config:
    path = path or config_path()
    if not path.is_file():
        raise ConfigError(
            f"no config at {path}\n  create a starter one with: fanctl config init"
        )

    try:
        with path.open("rb") as fh:
            raw = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: {exc}") from exc

    config = Config(path=path)

    for name, value in (raw.get("channels") or {}).items():
        if not isinstance(value, str):
            raise ConfigError(f"channels.{name}: expected a target string like 'hub:2'")
        config.channels[name] = value

    for name, value in (raw.get("profiles") or {}).items():
        config.profiles[name] = _parse_profile(name, value, config)

    if not config.profiles:
        raise ConfigError(f"{path}: no profiles defined")

    return config
