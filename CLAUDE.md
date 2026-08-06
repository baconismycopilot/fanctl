# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

`fanctl` is a rootless CLI and user daemon for the NZXT devices on this one machine, wrapping
liquidctl and OpenRGB. `README.md` documents the hardware findings and the measurements behind
the shipped curves; read it before changing anything thermal.

## Commands

```
make check                       # clean, lint, test -- run this before committing
make test                        # uv run --group dev pytest -q
make lint                        # ruff format, then ruff check
make install                     # uv tool install --editable . (puts fanctl on PATH)
make hooks                       # install the pre-commit ruff gate
make service                     # install + enable the systemd --user unit
make restart / logs / status     # control and watch the running daemon
```

The pre-commit hook runs ruff (check `--fix`, then format) on staged Python via
`uv run`, so it uses the ruff in `uv.lock` rather than a second one from
pre-commit's mirrors. It **fails the commit even when it fixed everything** —
fixes land in the working tree, not in what you staged, so you re-read and
`git add`. Tests deliberately stay out of it; `make check` is the full gate.

The tree is clean under `uvx pyright` and worth keeping that way, though pyright
is not wired into the hook or the Makefile — the liquidctl and OpenRGB surfaces
are dynamic enough that it earns its keep as an occasional check, not a gate.

CI (`.github/workflows/ci.yml`) runs the same two things on push and PR. Its job
names — **`lint`** and **`test`** — are required status checks on `main`, matched
by string: renaming a job without updating branch protection leaves `main`
waiting forever on a check that never reports. CI checks formatting rather than
applying it, so `ruff format` locally before pushing.

A single test needs the dev group, since pytest is not in the default dependencies:

```
uv run --group dev pytest tests/test_memory.py::test_indexed_token -q
uv run --group dev pytest -q -k smbus
```

Managed with `uv` throughout — never hand-roll a venv or a shell shim. `uv sync` creates `.venv`
for working without installing; `uv run fanctl status` then works directly.

**Nothing here uses sudo.** udev's uaccess ACLs cover both hidraw nodes and `/dev/bus/usb`, and the
daemon is a `systemctl --user` service. A change that needs privilege is a change that is wrong.

The installed CLI is editable, so source edits take effect immediately — but the *daemon* keeps
running the old code until `make restart`.

## Architecture

### Two backends, and the seam between them

| Function | Backend |
| --- | --- |
| Fans, pump, device-side curves, LCD, hub RGB | `backends/liquidctl_be.py` |
| Pump light ring, RGB memory | `backends/openrgb_be.py` (SDK on `127.0.0.1:6742`) |

liquidctl is imported as a **library**, not shelled out to, and `liquidctl==1.16.0` is pinned
because `caps.py` and `liquidctl_be.py` read private driver internals (`_speed_channels`,
`_color_channels`, `_COLOR_MODES`, the bucket helpers). Keep that dependency inside those two
modules so a breaking upgrade has a small blast radius.

`caps.py` **derives** capabilities from the installed driver classes rather than from a lookup
table — comparing bound methods against base implementations, since liquidctl signals
"unsupported" by inheriting a method that raises. A liquidctl upgrade that closes a gap therefore
shows up in `fanctl caps` immediately. Do not replace this with a hardcoded table.

### Three target namespaces

- `targets.resolve(devices, token, kind)` — anything backed by a liquidctl device (`hub:2`,
  `hub:fan2`, `pump`, `all`/`sync`). `kind` is `"fan"` or `"led"`, which is what makes the numeric
  shorthand `hub:2` mean `fan2` in a fans block and `led2` in a led block.
- `ring` — a pseudo-channel. liquidctl exposes no colour channel on the Kraken 2024 Elite RGB, so
  `caps.ring_zone` routes it to OpenRGB while it still resolves through `targets`.
- `memory.py` — bypasses device discovery entirely. A DIMM has no liquidctl driver, no USB serial
  and no hidraw node, so `cli.cmd_led_set` dispatches `mem_mod.is_memory(target)` *before*
  `devices.discover()`.

### Never key on enumeration order

This recurs in four places and each has a different stable key: devices by **serial**
(`devices.py`), DIMMs by **SMBus address** parsed from the OpenRGB location string
(`openrgb_be.find_memory` — a property of the slot), hwmon by the **`name` file** (`temps.py`,
never the hwmon index), LCD/curve behaviour by declared capability rather than product guesswork.

### What the daemon schedules

`Daemon._manage()` is the single decision point: a curve is scheduled in userspace **unless** the
device can run it itself *and* the curve's `source` matches what that device can interpolate
against. The Control Hub has no device-side curves at all; the Kraken has them but reads only its
own liquid probe, so a `source = "cpu"` pump curve is scheduled here and a `source = "liquid"` one
is written to the cooler once at startup. `apply` reports every curve it had to skip rather than
silently doing less than the config asks.

Other daemon invariants: no device handle is held across polls (so `fanctl status` never blocks on
the daemon); suspend is *detected* by comparing `CLOCK_BOOTTIME` to `CLOCK_MONOTONIC`, since sleep
hooks need root; shutdown raises every managed channel to `fallback_duty`, floored per channel.

### Silent failure is the recurring hazard

Exit code 0 proves very little on this hardware, and most of the defensive code exists for that
reason:

- LCD uploads: `_setup_bucket`/`_switch_bucket` only `_LOGGER.error` and never raise, so
  `liquidctl_be` installs a log handler to turn those into `ScreenUploadError`.
- OpenRGB colour models: `MODE_SPECIFIC` colours ride on the mode, `PER_LED` colours go to the
  zone's LED buffer. Writing to the wrong one is accepted and does nothing.
- OpenRGB speed ranges are not always ascending — the ring runs 0→4, the ENE DRAM controller 4→0,
  so `_scale_speed` interpolates in whichever order the mode declares.
- Temperature reads: hidraw does not guarantee a response belongs to your request. `temps.PLAUSIBLE`
  discards impossible values and the channel holds its last duty.

**When a change touches RGB or the LCD, ask the user to confirm what the hardware actually shows.**
A successful SDK readback is not evidence.

### Everything else worth knowing

- Every device access goes through `lock.device_lock(serial)`. It serialises only *our* processes;
  `coolercontrold` must stay disabled.
- `caps.PUMP_FLOOR = 30` / `FAN_FLOOR = 20` are **policy**, not hardware limits — they catch a
  mistyped `5` for `50`. `--force` overrides.
- Curves fitted by `calibrate` are measured against coolant temperature, which is the right
  variable for radiator fans and the wrong one for the pump. See the README section on this before
  touching pump curves.
- Config lives at `~/.config/fanctl/config.toml` (a working directory of this session), not in the
  repo. `config.generate_starter` emits only populated channels.

## Conventions

Comments here explain *why*, usually citing a measurement or an upstream bug, and are load-bearing
documentation of hardware behaviour that cannot be re-derived from the code. Match that when adding
to a module; do not add comments that restate the line below them.

Test docstrings carry the same weight — `tests/test_daemon_pump.py` and `tests/test_memory.py` open
by stating what hardware fact the file is pinning down.

Commit messages: subject line plus a line or two at most. Durable reasoning belongs in a code
comment or the README, not in git history.

Ruff config is pinned in `pyproject.toml` (line length 100, `E F W I UP B RUF`). Bandit's `S` rules
are deliberately excluded — several except-and-carry-on blocks here are intentional and commented.
