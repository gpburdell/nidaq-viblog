# Hardware & API notes — NI-9234 / cDAQ / nidaqmx

Drafted 2026-08-02 from vendor documentation knowledge plus the nidaqmx-python
docs (readthedocs, reviewed) and NI KB "Using a NI DAQ Device with Python and
NI-DAQmx in Windows" (reviewed). **NI's spec pages are JS-rendered and were not
directly fetchable in the planning session — items marked (verify) must be
checked against the NI-9234 datasheet/manual and the phase-1 bench spike
before being relied on.** This mirrors how the G-Link/MSCL notes were built:
plan from documentation, verify every load-bearing number on the bench.

## NI-9234 module

- 4 simultaneously-sampled channels, **24-bit delta-sigma** ADCs, ±5 V input (verify)
- Max data rate **51.2 kS/s per channel**; rates derive from the internal
  master timebase: **fs = 13.1072 MHz / 256 / n, n = 1..31** → 51.2k, 25.6k,
  17.067k, 12.8k, 10.24k, … , **2560 (n=20)** … down to **~1651.6 S/s minimum**
  (verify n range). Requested rates are **coerced to the ladder** — always read
  back `task.timing.samp_clk_rate`.
  - Consequence: **no native 512/1024/2048 Hz.** For VC/DG11 bands (≤100 Hz)
    acquire at 2560 S/s (matches the NIH reference datasets exactly:
    `wf_increment = 0.000390625 = 1/2560`) and decimate in software if wanted.
- **IEPE excitation ~2 mA** per channel, software-selectable per channel (verify
  exact value/options); AC or DC coupling per channel, AC cutoff **~0.5 Hz** (verify)
- Anti-aliasing is inherent to the delta-sigma converters and tracks the data
  rate (alias-free bandwidth ≈ 0.45 × fs) (verify) — no user filter choices,
  unlike the G-Link's LPF ladder
- **TEDS** supported (verify) — can read sensor sensitivity electronically
- Chassis: cDAQ (USB: 9171 one-slot, 9174 four-slot, 9178 eight-slot;
  Ethernet: 9185/9188) and cRIO. Channels of multiple modules in **one task
  share the sample clock** → simultaneous sampling across modules.
- Timebase accuracy **±50 ppm** (verify) → ~4.3 s/day absolute drift vs a
  disciplined host clock; relative timing within a task is exact.

## Sensor reality check (PCB 393B05 — the NIH sensor)

Seismic ICP/IEPE accelerometer, **10 V/g** sensitivity → on a ±5 V input the
measurement range is **±0.5 g**, noise floor ~µg-class (that's why the NIH
wired data resolves two decades below the G-Link MEMS floor at low frequency).
Implications: trigger thresholds above ~0.4 g are unreachable (config-time
warning), and clipping is conceivable for heavy impacts — the metrics window
peak sitting at the rail is the tell.

## nidaqmx-python essentials (reviewed on readthedocs)

- ctypes wrapper over the NI-DAQmx C API; Windows + Linux; **CPython 3.9+**;
  requires the NI-DAQmx driver or runtime on the machine
  (`python -m nidaqmx installdriver` bootstraps it)
- Core objects: `nidaqmx.Task` (channels + timing + triggers),
  `task.ai_channels.add_ai_accel_chan(...)` (IEPE accel channel: physical
  channel, sensitivity in mV/g, units g, excitation source/current),
  `task.timing.cfg_samp_clk_timing(rate, sample_mode=AcquisitionType.CONTINUOUS,
  samps_per_chan=<host buffer size>)`
- High-performance reads: `nidaqmx.stream_readers.AnalogMultiChannelReader`
  into a **preallocated numpy array** (`read_many_sample`); poll
  `task.in_stream.avail_samp_per_chan`, or use the
  `register_every_n_samples_acquired_into_buffer_event` callback. The polling
  model matches viblog's runner cadence and keeps the loop single-threaded —
  preferred.
- Buffer discipline: `samps_per_chan` in continuous mode sizes the host
  buffer — size it to **several seconds minimum**; failing to drain fast
  enough raises the classic **-200279 "attempted to read samples that are no
  longer available"** (verify exact code on this stack) = permanent loss →
  must land in the gap ledger and the health strip, exactly like radio loss
  in viblog
- Device discovery: `nidaqmx.system.System.local().devices` (names match NI
  MAX, e.g. `cDAQ1Mod1`); module `product_type`, serial readable for
  session.json identity
- Timestamps are the host's job: DAQmx provides sample counts, not wall-clock
  stamps — anchor `t0 = time.time_ns()` at task start (refine on first read)
  and compute `t = t0 + k/fs`

## Wired-vs-wireless completeness model

| Risk | Wireless (viblog) | Wired (this project) |
|---|---|---|
| Transport loss | radio dropouts; lossless retransmit; tick gaps | none in normal operation |
| Backpressure | MSCL 100k-packet ring silently overwrites | DAQmx buffer → hard error -200279 (loud, but data is gone) |
| Silent stop | lost-beacon timeout | USB unplug / chassis power → task exception |
| Proof of completeness | tick continuity + diagnostics | cumulative sample counter continuity + zero task errors |

## To fill in during phase 1 (bench)

- Confirmed rate ladder as coerced by the driver
- Poll p95 / buffer high-water at 51.2 kS/s × 4 ch (Python throughput proof)
- Exact overflow error code + task state afterward + restart procedure
- IEPE bias voltages: healthy / open / shorted, per our sensors
- TEDS availability on the actual sensor inventory
- USB re-enumeration behavior and timing after disconnect
- Host-vs-timebase drift measurement over ≥1 h
