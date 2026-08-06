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
fanctl led set memory fixed 00ccff     # both DIMMs
fanctl led set memory:2 chase ff0000 --speed slower

fanctl lcd brightness 80
fanctl lcd liquid
fanctl lcd image ~/pic.png                # 640x640; initialises the device first

fanctl config init                     # starter config from discovered hardware
fanctl profile list
fanctl apply balanced --dry-run         # show what would change, touch nothing
fanctl apply balanced

fanctl daemon --dry-run                 # log curve decisions without writing
systemctl --user start fanctl           # run it for real

fanctl calibrate radiator               # measure the loop, fit a curve to it
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
| RGB memory | OpenRGB SDK on `127.0.0.1:6742` | G.Skill DIMMs, ENE controller on the SMBus |

The Kraken 2024 Elite **RGB** cannot set a colour through liquidctl at all:
`kraken3.py` maps product `0x3012` to an empty colour-channel dict, and the
upstream guide states that controlling the light ring is not yet supported.
OpenRGB drives it as a generic Hue 2 device on the same hidraw node — 24 LEDs on
zone 0. Conversely OpenRGB fails to size the hub's F240 accessory (reports 0
LEDs) where liquidctl detects it correctly, so hub RGB stays on liquidctl.

Memory is OpenRGB-only by nature rather than by gap: the DIMMs answer on the
SMBus at their SPD addresses, so there is no USB device for liquidctl to find.

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

- **RGB memory** — two G.Skill DIMMs, 8 LEDs each, on the SMBus at `0x71` and
  `0x73`.

## RGB memory

Memory is the one target that does not go through device discovery. There is no
liquidctl driver behind a DIMM, no USB serial to key an alias on, and no hidraw
node for `lock.py` to guard, so it gets its own namespace instead of being forced
through `targets.resolve()`.

```
fanctl led set memory fixed 00ccff       # every stick
fanctl led set memory:1 fixed ff0000     # one stick
fanctl led set memory off
```

`memory`, `mem`, `ram` and `dram` all mean the same thing. Sticks are numbered
by **SMBus address**, not by OpenRGB's detection order, so `memory:1` keeps
meaning the same DIMM across reboots. `fanctl caps` prints the addresses. Which
physical slot that is, only looking at the machine will tell you.

Fewer colours than LEDs is not an error — they cycle across the stick, so
`fanctl led set memory fixed ff0000 0000ff` gives an alternating pattern down
the eight LEDs.

The controller disagrees with the pump ring on two things worth knowing, since
both are silent failures rather than errors:

- Its modes are `PER_LED`, so colours go to the zone's LED buffer. The ring's
  are `MODE_SPECIFIC` and carry their own palette. Writing to the wrong one is
  accepted and does nothing.
- Its speed scale runs **backwards** — `speed_min` 4 down to `speed_max` 0,
  where the ring runs 0 up to 4. Clamping to those endpoints pinned every DRAM
  animation to one speed, so the canonical name is interpolated across the range
  in whichever order the mode declares it.

It also has a real `Off` mode, where the ring has none and off is emulated by
holding every LED black.

## Profiles

`~/.config/fanctl/config.toml` holds named profiles — whole-machine states
covering fan duties, a pump curve, lighting on both backends, and the LCD.
`fanctl config init` writes a starter file from whatever is actually plugged in,
skipping empty headers.

Channel aliases are the point of the file:

```toml
[channels]
radiator = "hub:2"     # F240, exhaust over the radiator
intake   = "hub:4"     # F360, front intake
ring     = "ring"
memory   = "memory"    # both DIMMs; memory:1 / memory:2 address one
```

The numeric shorthand is deliberate: `hub:2` resolves to `fan2` in a `fans`
block and `led2` in a `led` block, so one alias names the physical thing once
and serves both. The device cannot tell you what a fan is bolted to, so this is
where that knowledge lives.

A fan takes either a fixed duty or a curve:

```toml
[profiles.balanced.fans]
radiator = { curve = [[30, 30], [40, 70]], source = "liquid" }
intake   = 40
```

Curves are accepted in config today but **the Control Hub cannot run them** — it
has no device-side curve support, so they need the daemon. `apply` reports each
one it had to skip rather than silently doing less than the config asks:

```
skipped: hub:fan2 curve needs the daemon (no device-side curve support)
skipped: kraken:pump curve wants 'cpu' but the device can only follow 'liquid'
```

The pump's own curve runs on the cooler and works today. Use `--dry-run` before
trusting a new profile, and `--force` to override the duty floors.

## The daemon

The Control Hub can hold a fixed duty and nothing else, so anything
temperature-reactive on those channels is scheduled from userspace. The pump's
curve runs on the cooler itself and is written once at startup.

```
cp systemd/fanctl.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now fanctl
journalctl --user -u fanctl -f
```

It runs the profile named by `profile` under `[daemon]`. Rootless, as a user
service — nothing here needs privilege.

```toml
[daemon]
profile = "adaptive"
interval = 2.0        # temperature poll, seconds
lcd_interval = 5.0    # floor on LCD pushes, seconds
min_step = 3          # smallest duty change worth writing
fallback_duty = 60    # written to managed channels on shutdown
```

Behaviours worth knowing:

- **Damping is asymmetric.** A duty must move `min_step` before it is written,
  and spinning *down* needs twice that. Temperature drops the instant load does
  but returns just as fast, so a reluctant downward move stops fans sawing
  during bursty work.
- **Implausible temperatures are discarded.** A status read issued right after
  a burst of writes returned a liquid temperature of 4°C on this hardware —
  hidraw does not guarantee a response belongs to your request. Readings outside
  a sane band are dropped and the channel holds its last duty for that poll.
- **Suspend is detected, not hooked.** Sleep hooks are system-level and this
  runs rootless, so the loop compares `CLOCK_BOOTTIME` against
  `CLOCK_MONOTONIC`; only the former advances across suspend. Since these
  devices keep no state through a power cycle, everything is re-applied on wake.
- **Stopping raises the fans** to `fallback_duty`. Once the daemon is gone
  nothing manages cooling and the hub holds its last duty forever, which is a
  quiet trap if that duty was an idle 25%.

## Calibration

A curve is only doing work over the range the loop actually occupies. Points
below idle never engage; points above the hottest it ever gets are decoration.
That range cannot be guessed — it depends on radiator area, ambient, pump speed
and how much heat the CPU really dumps — so `calibrate` measures it:

```
fanctl calibrate radiator --csv ~/soak.csv
```

It records an idle baseline, then loads every core and holds the fans at a
fixed duty while the coolant settles, twice: once at the floor and once at full
speed. It prints what it measured and a curve fitted to it.

**Why it pins the fans.** Loading the machine and watching the coolant settle
measures one point of a feedback loop — the temperature is only that because
the fans were running at whatever duty the *current* curve chose. Fit a new
curve to that number and you have used the old curve's behaviour to justify the
new one. Holding the fans still removes the feedback, and running both ends
gives the two temperatures that matter:

| Phase | Meaning |
| --- | --- |
| Load, fans at floor | the hottest the loop gets when the curve is not helping — where a curve should be asking for everything |
| Load, fans at full | the coldest it can be held under that load — below this, more airflow buys nothing |

Everything a curve can change happens between those two. On this machine the
gap is only a few degrees, which is why the fitted curve is much steeper than
it looks like it should be. That is correct: coolant temperature is heavily
damped, so a steep curve on liquid does not produce the hunting that the same
slope on a CPU sensor would.

The run takes about half an hour by default (`--idle`, `--phase`, `--cooldown`
shorten it) and the machine is loud throughout. Sampling continues after the
load stops, because if the loop had not yet plateaued its peak arrives minutes
*late* and a curve fitted without it will be caught out.

The `fanctl` service is stopped for the duration and restarted afterwards —
otherwise it would overwrite every pinned duty. That restart happens on Ctrl-C
and on failure too, along with restoring `fallback_duty` to the channel, so an
interrupted run never leaves the fans pinned with nothing managing them. Pass
`--no-manage-daemon` to handle the service yourself.

Nothing is written to the config: the suggested curve is printed for you to
paste, because the temperature it was fitted at is worth reading before you
adopt it.

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

### Stat frames

`lcd.mode = "stats"` renders temperatures and speeds on the host and uploads
them, since the firmware has no widget for any of them:

```toml
[profiles.adaptive.lcd]
mode = "stats"
sources = ["cpu", "gpu", "pump", "radiator"]
```

Up to four sources. Temperatures (`cpu`, `gpu`, `liquid`) render in °C with
colour bands — blue, amber past 65, red past 80. Speeds render in RPM without
bands, since a high RPM is not a problem the way a high temperature is:
`pump`, or **any fan channel alias** for that fan's speed.

One to three sources stack as rows; four switch to a 2×2 grid, because the
bottom row of a four-row stack lands where the round panel has narrowed to
under half its width. Value text auto-shrinks to fit its cell — a four-digit
RPM is more than twice the width of a two-digit temperature.

Frames are only pushed when a value moves beyond a threshold (1°C, 30 RPM)
rather than on any change at all. Fan and pump RPM jitter by a couple of counts
between polls, and treating that as a change would push a frame on almost every
interval forever. That matters:
each upload is a ~1.6 MB bulk transfer that claims one of **16 asset buckets**,
and those buckets persist across processes and reboots. Exhaust them and every
subsequent frame fails — which liquidctl reports by logging, never by raising,
so an upload that never reached the panel returns exactly like one that did.

fanctl handles this four ways: stale buckets are pruned after each successful
push (occupancy holds at 1/16), startup reclaims whatever earlier runs left
behind, uploads retry up to three times with a pause and a reclaim between
attempts, and liquidctl's error log is watched so a silent failure surfaces as a
real exception (`ScreenUploadError`) instead of a false success.

Setup and switch still fail intermittently even with buckets free, and recover
on the next attempt — retrying immediately tends to fail again, hence the
pauses. This is upstream issue #774. A frame that fails all three attempts is
logged and retried on the next interval; the panel just holds the previous
frame.

Recovered attempts do not reach the log at `INFO`. liquidctl reports these by
logging `ERROR` directly, and at the rate this firmware produces them that
trains you to scroll past the one line that matters, so propagation is
suppressed for the duration of an upload — a recovered attempt appears only
under `-v`, while a frame that exhausts all three attempts still surfaces as a
warning carrying liquidctl's own messages.

### Pixel format

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

The OpenRGB SDK server is required for the ring and for memory:

```
systemctl --user status openrgb-server
```

Memory additionally needs the `i2c-dev` module loaded and an OpenRGB build with
I2C detection enabled — without it the DIMMs are simply absent from `fanctl
caps` rather than failing loudly.

## Setup

Managed with [uv](https://docs.astral.sh/uv/). `make help` lists everything;
the common ones:

```
make install     install the CLI onto PATH, editable
make service     install and enable the user unit
make logs        follow the daemon's journal
make restart     restart the daemon after a config change
make check       clean, lint and test
```

Or directly:

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

All four phases complete: contention cleared, read-only enumeration, writes
across every writable surface, TOML profiles with `apply`, and the curve daemon
with stat frames on the LCD.

Known rough edges:

- Curve `source` accepts `cpu`, `gpu` and `liquid`. Only `liquid` can run
  on-device (pump only); everything else needs the daemon running.
- The daemon re-applies on resume by detecting the suspend rather than being
  woken by a hook. A hook would need root.
- LCD bucket exhaustion is worked around, not fixed. The upstream bug is open.
