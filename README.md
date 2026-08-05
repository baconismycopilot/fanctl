# fanctl

Unified CLI for the NZXT devices on this machine.

```
fanctl status                          # live readings from every device
fanctl caps                            # what each device can and cannot do, per backend
fanctl init                            # detect accessories after a cold boot

fanctl fan set hub:2 55                # also hub:fan2, or hub:all for every channel
fanctl pump set 40
fanctl pump curve 30:40 40:70 50:100   # on-device, liquid temperature

fanctl led set hub:2 fixed 00ff00      # -> liquidctl
fanctl led set ring spectrum-wave      # -> openrgb, same syntax
fanctl led set ring fading ff0000 0000ff --speed faster
fanctl led set ring off

fanctl lcd brightness 80
fanctl lcd liquid
fanctl lcd image ~/pic.png                # 640x640; initialises the device first
```

Colours accept `ff0000`, `#ff0000`, `rgb(255,0,0)`, `hsv(0,100,100)` or
`hsl(0,100,50)`. Mode names are canonical across both backends —
`fanctl caps` lists which ones a given target actually implements, since the hub
supports six and the ring fifteen.

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
  Channels 1, 3 and 5 are empty. Both fan groups are wired here, not to the
  Kraken.
  - `fan2`/`led2` — F240 RGB Core, **on the AIO radiator, exhaust**. Keep the
    duty floor on this channel and tie it to liquid temperature.
  - `fan4`/`led4` — F360 RGB Core, **front intake**. Free to run quieter, and a
    reasonable candidate for a CPU- or ambient-driven curve rather than liquid.

The AIO's own bundled PWM fans are not in use, which is why the Kraken's `fan`
channel reads 0 rpm. Nothing should ever be scheduled onto it.

Zone 0 on the Kraken is the pump light ring — confirmed visually, and it is the
only populated zone on that device.

## The LCD

`set_screen` accepts exactly five modes on this product: `liquid`, `brightness`,
`orientation`, `static` and `gif`. **`liquid` is the only built-in stat
readout** — the firmware has no CPU or GPU widget. When NZXT CAM displays those,
it is rendering frames on the host and uploading them, so showing anything other
than liquid temperature means generating 640×640 images ourselves.

`lcd image` **works** on this unit (Kraken 2024 Elite, firmware 1.2.0), verified
with a full-screen four-quadrant test pattern: all quadrants and a border at the
extreme edges rendered, so there is no cropping or scaling loss. Host-rendered
stat frames are therefore viable.

Two things worth knowing before you debug this path:

- **`fanctl lcd image` initialises the device first.** Upstream is explicit that
  devices must be initialised before being written to, and an early upload here
  rendered only the centre of the image. The init is cheap next to a 1.6 MB
  transfer.
- **Test with a bright, full-bleed image.** A dark image with fine detail is a
  terrible diagnostic — it is genuinely hard to tell a failed upload from a
  correctly rendered dark image. `_setup_bucket` and `_switch_bucket` only
  `_LOGGER.error` on failure and never raise, so a zero exit code proves
  nothing about what reached the panel.

Do not "fix" this by forcing liquidctl's RGB565 path
(`_prepare_static_file_rgb16` + `_send_2023_data_fw2`), which upstream gates on
product `0x300E` with firmware major 2. On `0x3012` it produces a black screen;
the default RGBA path is correct.

## Safety floors

`fan set` and `pump curve` refuse a duty below a policy floor: **30% pump, 20%
fan**. These are not hardware limits (liquidctl permits a 20% pump); they guard
against a mistyped `5` for `50`, which is silent until the liquid temperature
has already climbed. `--force` overrides.

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

Phases 0-2 complete: contention cleared, read-only enumeration, and writes for
fans, pump, on-device pump curve, both lighting backends, and the LCD.

Not yet built: TOML profiles and `apply` (phase 3), and the curve daemon with
its systemd unit and resume hook (phase 4). Until the daemon exists, hub fans
hold whatever fixed duty you last set — nothing is temperature-reactive except
the pump's on-device curve.
