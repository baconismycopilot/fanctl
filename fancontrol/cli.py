"""fanctl command line interface."""

from __future__ import annotations

import argparse
import sys

from . import __version__
from . import caps as caps_mod
from . import devices as dev_mod
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


def cmd_status(args) -> int:
    found = dev_mod.discover()
    if not found:
        print("no supported NZXT devices found", file=sys.stderr)
        return 1

    for i, device in enumerate(found):
        if i:
            print()
        header = f"{device.label}"
        print(f"{_style(header, BOLD)}  {_style('[' + device.alias + ']', DIM)}")
        print(_style(f"  {device.serial}  {device.hidraw or ''}", DIM))

        try:
            readings = liquidctl_be.read_status(device)
        except Exception as exc:  # noqa: BLE001 - report per device, keep going
            print(f"  error reading status: {exc}", file=sys.stderr)
            continue

        rows = [(r.key, *_fmt_value(r), r.is_empty_channel) for r in readings]
        key_w = max((len(k) for k, _, _, _ in rows), default=0)
        val_w = max((len(v) for _, v, _, _ in rows), default=0)
        for key, value, unit, empty in rows:
            note = _style("   (nothing connected)", DIM) if empty else ""
            print(f"  {key:<{key_w}}  {value:>{val_w}} {unit:<3}{note}")

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

        if caps.ring_zone is not None:
            if ring is not None:
                print(
                    f"  {'pump ring':<18} zone {ring.zone_id} "
                    f"({ring.leds} LEDs)  [openrgb]"
                )
            else:
                print(f"  {'pump ring':<18} present, but OpenRGB not reachable  [openrgb]")

        print(f"  {'lcd screen':<18} {'yes' if caps.screen else 'no'}")

        if caps.gaps:
            print(f"  {_style('gaps', DIM)}")
            for gap in caps.gaps:
                print(_style(f"    - {gap}", DIM))

    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fanctl",
        description="Unified control for NZXT devices via liquidctl and OpenRGB.",
    )
    parser.add_argument("--version", action="version", version=f"fanctl {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_status = sub.add_parser("status", help="show live readings for every device")
    p_status.set_defaults(func=cmd_status)

    p_caps = sub.add_parser(
        "caps", help="show what each device can and cannot do, and which backend owns it"
    )
    p_caps.set_defaults(func=cmd_caps)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except dev_mod.DeviceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
