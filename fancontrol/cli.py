"""fanctl command line interface."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from . import caps as caps_mod
from . import colors as color_mod
from . import devices as dev_mod
from . import targets as target_mod
from .backends import liquidctl_be, openrgb_be

DIM = "\033[2m"
BOLD = "\033[1m"
RESET = "\033[0m"


def _style(text: str, code: str) -> str:
    return text if not sys.stdout.isatty() else f"{code}{text}{RESET}"


def _fmt_value(reading: liquidctl_be.Reading) -> tuple[str, str]:
    """Split into (value, unit) so numbers align on their own column."""
    value = reading.value
    if value is None:
        # liquidctl reports an unpopulated channel's control mode as None.
        return "-", ""
    if isinstance(value, float):
        value = f"{value:.1f}"
    return str(value), reading.unit


def _print_readings(readings: list[liquidctl_be.Reading], indent: str = "  ") -> None:
    rows = [(r.key, *_fmt_value(r), r.is_empty_channel) for r in readings]
    key_w = max((len(k) for k, _, _, _ in rows), default=0)
    val_w = max((len(v) for _, v, _, _ in rows), default=0)
    for key, value, unit, empty in rows:
        note = _style("   (nothing connected)", DIM) if empty else ""
        print(f"{indent}{key:<{key_w}}  {value:>{val_w}} {unit:<3}{note}")


def _check_duty(channel: str, duty: int, force: bool) -> None:
    """Enforce the policy duty floor unless explicitly overridden."""
    if not 0 <= duty <= 100:
        raise caps_mod.FloorError(f"duty must be 0-100, got {duty}")
    floor = caps_mod.duty_floor(channel)
    if duty < floor and not force:
        raise caps_mod.FloorError(
            f"{duty}% is below the {floor}% safety floor for {channel!r}; "
            "pass --force if you mean it"
        )


# --------------------------------------------------------------------------
# read-only commands


def cmd_status(args) -> int:
    found = dev_mod.discover()
    if not found:
        print("no supported NZXT devices found", file=sys.stderr)
        return 1

    for i, device in enumerate(found):
        if i:
            print()
        print(f"{_style(device.label, BOLD)}  {_style('[' + device.alias + ']', DIM)}")
        print(_style(f"  {device.serial}  {device.hidraw or ''}", DIM))
        try:
            _print_readings(liquidctl_be.read_status(device))
        except Exception as exc:  # noqa: BLE001 - report per device, keep going
            print(f"  error reading status: {exc}", file=sys.stderr)
    return 0


def cmd_caps(args) -> int:
    found = dev_mod.discover()
    if not found:
        print("no supported NZXT devices found", file=sys.stderr)
        return 1

    client = None
    try:
        client = openrgb_be.connect()
    except openrgb_be.OpenRGBUnavailable as exc:
        print(_style(f"warning: {exc}", DIM), file=sys.stderr)
        print(file=sys.stderr)

    for i, device in enumerate(found):
        if i:
            print()
        print(f"{_style(device.label, BOLD)}  {_style('[' + device.alias + ']', DIM)}")

        caps = caps_mod.inspect_device(device)
        ring = None
        if caps.ring_zone is not None and client is not None:
            ring = openrgb_be.find_ring(client, device.serial, caps.ring_zone)
            caps_mod.enrich_with_openrgb(device, caps, ring)

        print(f"  {'speed channels':<18} {', '.join(caps.speed_channels) or '-'}")
        if caps.speed_profile:
            src = caps.profile_temp_source or "device"
            print(f"  {'device curves':<18} yes  ({src} temperature)")
        else:
            print(f"  {'device curves':<18} {_style('no', BOLD)}  (daemon required)")

        colour = ", ".join(caps.color_channels) or _style("none", BOLD)
        print(f"  {'colour channels':<18} {colour}  [liquidctl]")
        lq_modes = caps_mod.liquidctl_modes(device)
        if lq_modes:
            print(_style(f"  {'  modes':<18} {', '.join(lq_modes)}", DIM))

        if caps.ring_zone is not None:
            if ring is not None:
                print(f"  {'pump ring':<18} zone {ring.zone_id} ({ring.leds} LEDs)  [openrgb]")
                modes = caps_mod.modes_for(device, caps, "ring")
                print(_style(f"  {'  modes':<18} {', '.join(modes)}", DIM))
            else:
                print(f"  {'pump ring':<18} present, but OpenRGB not reachable  [openrgb]")

        print(f"  {'lcd screen':<18} {'yes' if caps.screen else 'no'}")

        if caps.gaps:
            print(f"  {_style('gaps', DIM)}")
            for gap in caps.gaps:
                print(_style(f"    - {gap}", DIM))
    return 0


def cmd_init(args) -> int:
    """Initialise every device; required after a cold boot or hardware change."""
    found = dev_mod.discover()
    if not found:
        print("no supported NZXT devices found", file=sys.stderr)
        return 1

    for i, device in enumerate(found):
        if i:
            print()
        print(f"{_style(device.label, BOLD)}  {_style('[' + device.alias + ']', DIM)}")
        readings = liquidctl_be.initialize(device)
        if readings:
            _print_readings(readings)
        else:
            print(_style("  initialised", DIM))
    return 0


# --------------------------------------------------------------------------
# speed commands


def cmd_fan_set(args) -> int:
    found = dev_mod.discover()
    target = target_mod.resolve(found, args.target, "fan")
    _check_duty("fan", args.duty, args.force)
    liquidctl_be.set_fixed_speed(target.device, target.channel, args.duty)
    print(f"{target}  duty -> {args.duty}%")
    return 0


def cmd_pump_set(args) -> int:
    found = dev_mod.discover()
    target = target_mod.sole_device_with(
        found, lambda c: "pump" if "pump" in c.speed_channels else None, "a pump channel"
    )
    _check_duty("pump", args.duty, args.force)
    liquidctl_be.set_fixed_speed(target.device, "pump", args.duty)
    print(f"{target}  duty -> {args.duty}%")
    return 0


def _parse_curve(points: list[str], channel: str, force: bool) -> list[tuple[int, int]]:
    parsed: list[tuple[int, int]] = []
    for point in points:
        if ":" not in point:
            raise ValueError(f"curve point {point!r} must be <temp>:<duty>, e.g. 40:50")
        raw_t, _, raw_d = point.partition(":")
        try:
            temp, duty = int(raw_t), int(raw_d)
        except ValueError as exc:
            raise ValueError(f"curve point {point!r} must be two integers") from exc
        _check_duty(channel, duty, force)
        parsed.append((temp, duty))

    if len(parsed) < 2:
        raise ValueError("a curve needs at least two points")
    temps = [t for t, _ in parsed]
    if temps != sorted(temps) or len(set(temps)) != len(temps):
        raise ValueError("curve temperatures must be strictly ascending")
    return parsed


def cmd_pump_curve(args) -> int:
    found = dev_mod.discover()
    target = target_mod.sole_device_with(
        found, lambda c: "pump" if "pump" in c.speed_channels else None, "a pump channel"
    )
    if not target.caps.speed_profile:
        print(
            f"error: {target.device.alias} has no device-side curves; "
            "use the daemon instead",
            file=sys.stderr,
        )
        return 2

    profile = _parse_curve(args.points, "pump", args.force)
    liquidctl_be.set_speed_profile(target.device, "pump", profile)
    source = target.caps.profile_temp_source or "device"
    shown = "  ".join(f"{t}C:{d}%" for t, d in profile)
    print(f"{target}  curve -> {shown}   ({source} temperature, on-device)")
    return 0


# --------------------------------------------------------------------------
# lighting commands


def cmd_led_set(args) -> int:
    found = dev_mod.discover()
    target = target_mod.resolve(found, args.target, "led")
    mode = args.mode.strip().lower()

    if mode not in color_mod.MODES:
        known = ", ".join(sorted(color_mod.MODES))
        print(f"error: unknown mode {mode!r}; known modes: {known}", file=sys.stderr)
        return 2

    parsed_colors = [color_mod.parse_color(c) for c in args.colors]
    backend = target.caps.backend_for_color(target.channel)

    if backend is caps_mod.Backend.OPENRGB:
        return _led_via_openrgb(target, mode, parsed_colors, args)
    if backend is caps_mod.Backend.LIQUIDCTL:
        return _led_via_liquidctl(target, mode, parsed_colors, args)

    print(f"error: {target} has no colour control on any backend", file=sys.stderr)
    return 2


def _led_via_liquidctl(target, mode: str, parsed_colors, args) -> int:
    available = caps_mod.liquidctl_modes(target.device)
    if mode not in available:
        print(
            f"error: {target.device.alias} does not implement mode {mode!r}; "
            f"available: {', '.join(available)}",
            file=sys.stderr,
        )
        return 2

    native = color_mod.canonical_for_liquidctl(mode)
    liquidctl_be.set_color(
        target.device,
        target.channel,
        native,
        parsed_colors,
        speed=args.speed,
        direction=args.direction,
    )
    print(f"{target}  {mode}{_fmt_colors(parsed_colors)}  {_style('[liquidctl]', DIM)}")
    return 0


def _led_via_openrgb(target, mode: str, parsed_colors, args) -> int:
    try:
        client = openrgb_be.connect()
    except openrgb_be.OpenRGBUnavailable as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    ring = openrgb_be.find_ring(client, target.device.serial, target.caps.ring_zone)
    if ring is None:
        print(f"error: OpenRGB does not expose a ring for {target.device.alias}", file=sys.stderr)
        return 2

    caps_mod.enrich_with_openrgb(target.device, target.caps, ring)
    available = caps_mod.modes_for(target.device, target.caps, "ring")
    if mode not in available:
        print(
            f"error: the ring does not support mode {mode!r}; "
            f"available: {', '.join(available)}",
            file=sys.stderr,
        )
        return 2

    native = color_mod.canonical_for_openrgb(mode)  # None for 'off'
    speed_idx = color_mod.ANIMATION_SPEEDS.index(args.speed)
    direction_idx = color_mod.DIRECTIONS.index(args.direction)

    try:
        openrgb_be.set_ring(client, ring, native, parsed_colors, speed_idx, direction_idx)
    except openrgb_be.OpenRGBUnavailable as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(f"{target}  {mode}{_fmt_colors(parsed_colors)}  {_style('[openrgb]', DIM)}")
    return 0


def _fmt_colors(parsed_colors) -> str:
    if not parsed_colors:
        return ""
    return "  " + " ".join("#%02x%02x%02x" % tuple(c) for c in parsed_colors)


# --------------------------------------------------------------------------
# lcd commands


def _lcd_target(found):
    return target_mod.sole_device_with(
        found, lambda c: "lcd" if c.screen else None, "an LCD screen"
    )


def cmd_lcd(args) -> int:
    found = dev_mod.discover()
    target = _lcd_target(found)

    mode = args.lcd_mode
    value: str | None = None

    if mode in ("static", "gif"):
        path = Path(args.value).expanduser()
        if not path.is_file():
            print(f"error: no such file: {path}", file=sys.stderr)
            return 2
        value = str(path)
        # Upload goes over a bulk endpoint rather than hidraw, and upstream is
        # explicit that a device must be initialised before being written to.
        # A partial first upload here rendered only the centre of the image, so
        # initialise defensively -- it is cheap next to a 1.6 MB transfer.
        liquidctl_be.initialize(target.device)
    elif mode == "brightness":
        value = str(args.value)
        if not 0 <= int(args.value) <= 100:
            print("error: brightness must be 0-100", file=sys.stderr)
            return 2
    elif mode == "orientation":
        if int(args.value) not in (0, 90, 180, 270):
            print("error: orientation must be 0, 90, 180 or 270", file=sys.stderr)
            return 2
        value = str(args.value)

    liquidctl_be.set_screen(target.device, mode, value)
    shown = f" {value}" if value is not None else ""
    print(f"{target.device.alias}:lcd  {mode}{shown}")
    return 0


# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fanctl",
        description="Unified control for NZXT devices via liquidctl and OpenRGB.",
    )
    parser.add_argument("--version", action="version", version=f"fanctl {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("status", help="show live readings for every device")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser(
        "caps", help="show what each device can and cannot do, and which backend owns it"
    )
    p.set_defaults(func=cmd_caps)

    p = sub.add_parser("init", help="initialise devices and report attached accessories")
    p.set_defaults(func=cmd_init)

    # fan
    p_fan = sub.add_parser("fan", help="fan duty control")
    fan_sub = p_fan.add_subparsers(dest="fan_command", required=True)
    p = fan_sub.add_parser("set", help="set a fixed duty, e.g. 'fan set hub:2 55'")
    p.add_argument("target", help="channel, e.g. hub:fan2, hub:2, or hub:all")
    p.add_argument("duty", type=int, help="duty percentage")
    p.add_argument("--force", action="store_true", help="ignore the safety duty floor")
    p.set_defaults(func=cmd_fan_set)

    # pump
    p_pump = sub.add_parser("pump", help="pump duty and on-device curve")
    pump_sub = p_pump.add_subparsers(dest="pump_command", required=True)
    p = pump_sub.add_parser("set", help="set a fixed pump duty")
    p.add_argument("duty", type=int)
    p.add_argument("--force", action="store_true", help="ignore the safety duty floor")
    p.set_defaults(func=cmd_pump_set)
    p = pump_sub.add_parser(
        "curve",
        help="write an on-device curve, e.g. 'pump curve 30:30 40:50 50:100'",
    )
    p.add_argument("points", nargs="+", metavar="TEMP:DUTY")
    p.add_argument("--force", action="store_true", help="ignore the safety duty floor")
    p.set_defaults(func=cmd_pump_curve)

    # led
    p_led = sub.add_parser("led", help="lighting control across both backends")
    led_sub = p_led.add_subparsers(dest="led_command", required=True)
    p = led_sub.add_parser("set", help="set a lighting mode, e.g. 'led set ring fixed ff0000'")
    p.add_argument("target", help="channel, e.g. hub:led2, hub:sync, or ring")
    p.add_argument("mode", help="canonical mode name; see 'fanctl caps'")
    p.add_argument("colors", nargs="*", help="colours: ff0000, #ff0000, rgb(), hsv(), hsl()")
    p.add_argument(
        "--speed", choices=color_mod.ANIMATION_SPEEDS, default="normal", help="animation speed"
    )
    p.add_argument(
        "--direction", choices=color_mod.DIRECTIONS, default="forward", help="animation direction"
    )
    p.set_defaults(func=cmd_led_set)

    # lcd
    p_lcd = sub.add_parser("lcd", help="Kraken LCD screen")
    lcd_sub = p_lcd.add_subparsers(dest="lcd_command", required=True)
    p = lcd_sub.add_parser("brightness", help="set brightness, 0-100")
    p.add_argument("value", type=int)
    p.set_defaults(func=cmd_lcd, lcd_mode="brightness")
    p = lcd_sub.add_parser("orientation", help="set rotation: 0, 90, 180 or 270")
    p.add_argument("value", type=int)
    p.set_defaults(func=cmd_lcd, lcd_mode="orientation")
    p = lcd_sub.add_parser("image", help="display a still image")
    p.add_argument("value", metavar="PATH")
    p.set_defaults(func=cmd_lcd, lcd_mode="static")
    p = lcd_sub.add_parser("gif", help="display an animated GIF")
    p.add_argument("value", metavar="PATH")
    p.set_defaults(func=cmd_lcd, lcd_mode="gif")
    p = lcd_sub.add_parser("liquid", help="show the built-in liquid temperature readout")
    p.set_defaults(func=cmd_lcd, lcd_mode="liquid", value=None)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (dev_mod.DeviceError, target_mod.TargetError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except (caps_mod.FloorError, color_mod.ColorError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except TimeoutError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
