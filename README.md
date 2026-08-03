# nidaq-viblog — wired vibration logging & monitoring (NI DAQ / NI-9234)

Sibling project to **viblog** (`C:\Temp\mscl`, github.com/gpburdell/hbk_viblog),
providing the **same functions and features** — full-capture recording,
long-term monitoring with windowed metrics + triggered raw capture, live web
UI, session review, DG11/VC evaluation — but targeting **wired NI DAQ
hardware**: IEPE accelerometers (e.g. PCB 393B05) on an **NI-9234** C-Series
module in a cDAQ chassis, driven through **nidaqmx-python**.

## Why this is a thin project, not a rewrite

viblog was architected around a source-agnostic acquisition protocol
(`viblog.acquisition.types.SweepSource`): everything downstream — Parquet
sinks, session dirs, gap ledger, streaming DSP (1 s Hann windows, ANSI S1.11
1/3-octave, Lx percentile ladders, VC curves), metrics sink, trigger engine,
runtime control API, live web UI, and the HTML review — consumes that
protocol and is **reused unchanged**. This project contributes:

1. `NidaqSource` — a continuous nidaqmx acquisition task exposed through the
   viblog source protocol (plus one small upstream change: a chunk-native
   source path, see ARCHITECTURE §3)
2. A config schema for wired sensors (physical channels, sensitivity, IEPE,
   coupling, orientation) — the wired analog of SensorConnect-owned EEPROM
3. NI-specific completeness accounting (DAQmx buffer overflow = the wired
   equivalent of radio loss) and sensor health checks (IEPE bias)
4. A phase-1 bench spike harness for the real hardware

The validated NIH post-processing methodology already *came from* this
hardware family: the NIH wired datasets were 2560 S/s IEPE recordings — a
native NI-9234 data rate (13.1072 MHz / 256 / 20).

## Documents

| File | Contents |
|---|---|
| `docs/ARCHITECTURE.md` | Reuse strategy, wireless→wired concept mapping, NidaqSource design |
| `docs/WORKPLAN.md` | Phased plan with verification gates |
| `docs/HARDWARE_NOTES.md` | NI-9234 / nidaqmx essentials (flagged where bench verification is required) |

## Kickoff for the coding session

1. Read the three docs above, then skim viblog's `docs/ARCHITECTURE.md` and
   `src/viblog/acquisition/` (`types.py`, `sim.py`, `mscl_io.py`) — NidaqSource
   mirrors `mscl_io.py`'s role.
2. Resolve the open questions at the bottom of `docs/WORKPLAN.md` with the owner.
3. Phase 0 first (repo bootstrap + the small upstream viblog change), then the
   phase-1 hardware spike before building on top.

Stack: Python 3.13 + uv, `nidaqmx` (requires the NI-DAQmx driver/runtime on the
machine — `python -m nidaqmx installdriver`), viblog as a dependency.
