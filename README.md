# fanctl

Unified CLI for the NZXT devices on this machine.

```
fanctl status     # live readings from every device
fanctl caps       # what each device can and cannot do, and which backend owns it
```

## Why two backends

No single tool covers this hardware.

| Function | Backend | Devices |
| --- | --- | --- |
| Fans, pump, device curves, LCD, hub RGB | liquidctl (imported as a library) | Kraken `1e71:3012`, Control Hub `1e71:2022` |
| Pump light ring | OpenRGB SDK on `127.0.0.1:6742` | Kraken only |

The Kraken 2024 Elite **RGB** cannot set a colour through liquidctl at all:
`kraken3.py` maps product `0x3012` to an empty colour-channel dict, and the
upstream guide states that controlling the light ring is not yet supported.
OpenRGB drives it as a generic Hue 2 device on the same hidraw node — 24 LEDs on
zone 0. Conversely OpenRGB fails to size the hub's F240 accessory (reports 0
LEDs) where liquidctl detects it correctly, so hub RGB stays on liquidctl.

Run `fanctl caps` rather than trusting this table; it derives from the
installed drivers, so a liquidctl upgrade that closes a gap shows up immediately.

## Hardware layout

- **Kraken 2024 Elite RGB** — `<serial>`, `/dev/hidraw14`. Pump plus a
  light ring. Its own `fan` channel is empty; radiator fans are on the hub.
- **Control Hub** — `<serial>`, `/dev/hidraw2`.
  `fan2`/`led2` = F240 RGB Core, `fan4`/`led4` = F360 RGB Core. Channels 1, 3
  and 5 are empty.

## Contention

hidraw has no arbitration: two writers on one node cause dropped commands and
corrupt reads with nothing surfaced as an error. Every device access goes through
an advisory lock keyed on serial number (`lock.py`).

That only serialises *our* processes. `coolercontrold` must stay disabled — it
polls these nodes every second through its own embedded liquidctl:

```
systemctl is-enabled coolercontrold   # expect: disabled
```

The OpenRGB SDK server is required for ring control:

```
systemctl --user status openrgb-server
```

## Setup

Managed with [uv](https://docs.astral.sh/uv/). Install the CLI onto your PATH:

```
uv tool install --editable .
```

Editable, so source edits take effect without reinstalling. Drop `--editable`
for a normal install. To work on it without installing:

```
uv sync          # create .venv and install deps from uv.lock
uv run fanctl status
```

Everything comes from PyPI, including liquidctl and its native deps (`hidapi`,
`smbus`, `pyusb`) — there is no dependency on a system-installed liquidctl.
`uv.lock` is committed; treat it as the source of truth for what is deployed.

No root required. udev's `71-liquidctl.rules` grants uaccess on both hidraw
nodes and on `/dev/bus/usb` (needed for LCD image upload, which uses a bulk
endpoint). Motherboard `nct6799` headers *would* need root, which is why fanctl
stays NZXT-only and ships as a `systemctl --user` service.

## Status

Phases 0 and 1 are complete: contention cleared, read-only enumeration,
`status`, `caps`. Writes (phase 2), TOML profiles (phase 3), and the curve
daemon (phase 4) are not yet implemented.
