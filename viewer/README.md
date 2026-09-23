# Realtime viewer — sensitive-space demo (`vc_viewer.py`)

A standalone presentation tool: three live traces plus a live spectrum, with
**everything shown as velocity in micro-inches per second (µin/s)** against the
ASHRAE/IEST vibration criteria (VC) curves.

It does **not** touch the logging pipeline — no `viblog` import, no session
directory, no Parquet. It opens its own DAQmx task and writes nothing but
screenshots. The production config (`bench.yaml`, `src/nidaq_viblog/`) is
untouched and still accel-only; see "Known gap" below.

## Run it

```bash
# rehearse the talk with no hardware attached
uv run --no-project viewer/vc_viewer.py --sim

# the real bench (defaults match the hookup below)
uv run --no-project viewer/vc_viewer.py

# prove the DSP chain (no hardware, no GUI, exits non-zero on failure)
uv run --no-project viewer/vc_viewer.py --selftest
```

`--no-project` keeps uv from trying to build this repo's own environment (which
needs the `viblog` checkout at `../mscl`); the script declares its dependencies
inline (PEP 723), so uv fetches numpy/scipy/pyqtgraph/PyQt6/nidaqmx by itself.

## Default channel map

| Channel | Sensor | Sensitivity | IEPE | Clip |
|---|---|---|---|---|
| `cDAQ1Mod1/ai0` | laser vibrometer | 12.5 mV/(mm/s) | **off** | ±0.4 m/s = ±400 mm/s |
| `cDAQ1Mod1/ai1` | accelerometer | 1000 mV/g | 2 mA | ±5 g |
| `cDAQ1Mod1/ai2` | accelerometer | 1000 mV/g | 2 mA | ±5 g |

Those clip levels are the ±5 V NI-9234 input divided by the sensitivity — they
match the min/max the DAQExpress task was configured with, which is the quickest
confirmation that the sensitivities are entered correctly.

IEPE on the vibrometer is **off by default** (`--vel-iepe` turns it on). A
vibrometer's buffered voltage output normally must not be current-driven; the
DAQExpress screenshot showed excitation "Internal / 0.0000 A", which is the same
thing expressed differently.

Override anything: `--device`, `--vel-chans`, `--vel-sens`, `--accel-chans`,
`--accel-sens`, `--labels "Slab (laser),Column,Mid-bay"`.

## What it shows

**Left — three rolling time traces**, 5 s window (`--window`), all in µin/s, on
a common y-scale by default so the laser and the accelerometers can be compared
by eye. Each title carries live RMS and peak.

**Right — the spectrum**, log-log, two views toggled with `v`:

- **1/3-octave RMS velocity** (default) with the VC criteria drawn as dotted
  reference lines: ISO Workshop / Office / Residential / Operating theatre and
  VC-A…VC-E. This is the view that makes the sensitive-space argument — the
  bars either sit under a curve or they don't.
- **PSD** — velocity spectral density in µin/s/√Hz, for showing where the
  energy actually is (HVAC tones, footfall, machinery).

**Bottom** — the strictest VC criterion each channel currently meets, e.g.
`ai1 accelerometer: VC-C (372 µin/s)`, plus frame rate and DAQmx overflow count.

Keys: `space` pause · `v` PSD ↔ 1/3-octave · `p` peak-hold · `c` common vs
per-channel y-scale · `a` rescale now · `s` screenshot PNG · `q` quit.

## Signal path

```
laser : DAQmx velocity channel (m/s) → HP → ×3.9370079e7            → µin/s
accel : DAQmx accel channel (g) → ×9.80665 → HP → ∫dt → HP → ×3.9370079e7 → µin/s
```

Both high-pass stages are 4th-order Butterworth at `--hp` (default 1 Hz) run
with persistent filter state across blocks, so there are no per-block transients
and the integrator cannot drift away. The VC criteria are evaluated over the
8–80 Hz 1/3-octave bands (the flat-velocity part of each curve); the
constant-displacement extension below 8 Hz is not drawn.

`--selftest` feeds a 20 Hz, 1000 µin/s RMS motion through both paths — as
velocity and as its derivative — and requires each to come back at 1000 µin/s in
the trace RMS, in the 20 Hz band, and to classify VC-B:

```
path                        trace RMS     band RMS     VC  result
laser (velocity, m/s)         1,000.0      1,000.0   VC-B  PASS
accel (g, integrated)         1,005.7        999.8   VC-B  PASS
```

In `--sim` the accel channels carry the exact derivative of the laser channel's
velocity, so the three traces overlaying each other on screen is the same proof,
live. `--scenario vc-e|vc-c|vc-a|office` sets the level; footfall bursts repeat
every 6 s.

## Sample rate

`--rate` defaults to **2560 S/s**, which is native, covers the whole VC range
(≤100 Hz) with room to spare, and keeps the display cheap. Any rate must be on
the NI-9234 ladder (51200/n, n = 1…31) or the script refuses it and names the
rate the driver would have coerced it to. **25 000 S/s is not on the ladder —
use 25 600.** `--rate 25600` works fine here (traces are min/max-decimated to
`--trace-points` before drawing, so the frame rate doesn't change).

## Performance

~20 fps at either rate on a normal laptop. If it stutters on the presentation
machine: `--fps 15`, `--trace-points 1200`, or `--opengl` (GPU drawing, but
driver-dependent — test it before the talk). `--run-seconds N` closes the window
automatically, which is also how the unattended smoke test runs.

## Known gap (deliberate, for later)

The logging pipeline in `src/nidaq_viblog/` still only knows about
accelerometers: `backends.py` builds every channel with `add_ai_accel_chan` and
a mV/g sensitivity, so pointing `nidaq-viblog run` at ai0 would scale the
vibrometer as if it were a 12.5 mV/g accelerometer. Adding a velocity channel
type there (config schema, `ChannelPlan`, DAQmx channel creation, per-channel
units in `session.json`) is the next piece of work on the production system —
along with deciding how viblog's windowed metrics should treat a velocity
channel, since `processing.modalities` is currently a session-wide setting that
assumes acceleration in g.
