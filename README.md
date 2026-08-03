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

## Build status (coded 2026-08-02)

Phases 0 and the software half of phase 2 are implemented and green (20 tests);
what remains is bench validation on real hardware. See `docs/WORKPLAN.md` for the
per-item status. What exists now:

```
src/nidaq_viblog/
  rates.py         NI-9234 native rate ladder (51200/n) + coercion prediction
  config.py        wired YAML → (viblog SessionConfig, NidaqConfig) + validation
  backends.py      ReaderBackend: NidaqmxBackend (real, lazy nidaqmx) + SimBackend
  nidaq_source.py  NidaqSource — viblog SweepSource over one DAQmx task
  cli.py           nidaq-viblog run / init-config / devices
spike/             phase-1 bench scripts (01 inventory … 05 IEPE bias)
tests/             rate ladder, config, drain/overflow→gap, end-to-end via Runner
```

Design notes worth knowing:

- **Unmodified viblog.** `NidaqSource` emits per-sample `SweepData`; viblog's
  existing runner regroups them. The chunk-native upstream PR (ARCHITECTURE §3)
  is deferred as a throughput optimization, not a dependency.
- **Overflow → gap with no special-casing.** The per-module sample index is the
  device's own cumulative counter (`total_acquired − avail`), so a DAQmx buffer
  overflow (-200279) jumps the index → the synthesized uint16 tick jumps → the
  existing tick ledger records the gap. Proven deterministically in tests.
- **Hardware-free proof.** `NidaqSource` talks to a `ReaderBackend`, so the whole
  drain/ledger/sink path runs without the NI driver via `SimBackend`.
- **Doc correction:** 2048 S/s *is* a native rate (51200/25); the earlier
  "no native …/2048" note in HARDWARE_NOTES was wrong (see `test_rates.py`).

## Usage

```bash
uv sync                                            # installs viblog (../mscl) + nidaqmx
uv run nidaq-viblog init-config bench.yaml         # commented template
uv run nidaq-viblog devices                        # enumerate NI-DAQmx devices
uv run nidaq-viblog run --config bench.yaml --sim-nidaq --duration 5   # no hardware
uv run nidaq-viblog run --config bench.yaml        # real chassis (record/monitor per YAML)
uv run pytest                                      # 20 tests, no hardware needed
```

Everything after acquisition is viblog's and is source-agnostic — the live UI,
runtime control, and review run against the session directory unchanged:

```bash
viblog serve  --root sessions      #  live web UI + session browser
viblog ctl    status               #  runtime control of a running session
viblog review                      #  interactive HTML review of the latest session
```

## Kickoff for the coding session

1. Read the three docs above, then skim viblog's `docs/ARCHITECTURE.md` and
   `src/viblog/acquisition/` (`types.py`, `sim.py`, `mscl_io.py`) — NidaqSource
   mirrors `mscl_io.py`'s role.
2. Resolve the open questions at the bottom of `docs/WORKPLAN.md` with the owner.
3. **Next up: the phase-1 hardware spike** (`spike/01`–`05`), then the phase-2
   bench-validation gate (30-min record, clean ledger, review renders).

Stack: Python ≥3.11 + uv, `nidaqmx` (requires the NI-DAQmx driver/runtime on the
machine — `python -m nidaqmx installdriver`), viblog as a dependency.
