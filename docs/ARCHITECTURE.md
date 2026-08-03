# Architecture — nidaq-viblog (drafted 2026-08-02)

Target: same functions/features as viblog (record mode, monitor mode with
metrics + peak-acceleration trigger, live web UI, review, DG11/VC evaluation)
on wired NI DAQ hardware: **NI-9234** (4-ch IEPE, 24-bit) in a cDAQ chassis,
via **nidaqmx-python**. Single machine, one chassis per application instance.

## 1. Reuse strategy (decide in phase 0)

viblog is consumed as a **library dependency**; this repo contains only the
NI-specific parts. Options, in order of preference:

| Option | Mechanism | Notes |
|---|---|---|
| **A (recommended)** | uv git dependency: `viblog = { git = "https://github.com/gpburdell/hbk_viblog" }` | Clean versioning; corporate proxy may need `UV_SYSTEM_CERTS=1` |
| B | local path dependency on `C:\Temp\mscl` (`[tool.uv.sources] viblog = { path = ... , editable = true }`) | Best while both evolve on one machine; pair with A for reproducibility |
| C | vendor-copy `viblog/` subpackages | Last resort; forfeits upstream fixes |

Installing viblog pulls `pymscl` (harmless — a wheel exists; MSCL is simply
never imported unless a wireless source is used).

## 2. Concept mapping (wireless → wired)

| viblog / wireless | nidaq / wired equivalent |
|---|---|
| G-Link node (3 ch) | NI-9234 module (4 ch); `NodeInfo.address` = module index, channels = configured subset |
| WSDA gateway, TCP:5000 | cDAQ chassis (USB or Ethernet), NI-DAQmx driver |
| SensorConnect-owned EEPROM config | **YAML-owned sensor table** (physical channel, sensitivity mV/g, IEPE, coupling, orientation) + NI MAX for device naming; TEDS read where sensors support it |
| EEPROM snapshot in session.json | nidaqmx device/module identity (product type, serial) + the sensor table + actual task config readback |
| Beacon-disciplined per-sweep timestamps ±50 µs | host `time_ns()` anchored at first buffer + sample index / fs; chassis timebase ±50 ppm — relative timing within/across modules in one task is exact, absolute drift ~4 s/day is logged (health) |
| tick (uint16, wraps) | synthesized 64-bit cumulative sample counter (tick = counter & 0xFFFF to satisfy the existing ledger, plus the full counter kept in health) |
| Radio bandwidth precheck (`percentBandwidth`) | rate validation against the module's native rate ladder; USB/Ethernet bandwidth is a non-issue at vibration rates |
| Lossless retransmission / node FIFO | DAQmx host buffer (sized generously, seconds); **error -200279 buffer overwrite = permanent loss** → ledger gap entry + loud health flag; the drain loop's only hard job is to never let it happen |
| Lost-beacon silent stop | USB disconnect / chassis power loss → task error; handled by stop-ledger-restart with a recorded gap |
| Node diagnostic packets (battery, RSSI, ReTx) | **IEPE bias voltage check** (DC-coupled read at startup: ~8–12 V = healthy, ~0 V = short, ~rail = open/unpowered), buffer fill %, cumulative samples vs wall clock |
| `Log and Transmit` + flash reconcile | n/a — wired capture is single-path; completeness = zero overflow errors + contiguous counter |

Everything else — session dirs, raw/metrics/gaps/health Parquet sinks, ring
buffers, trigger engine (window peak g, pre/post-roll, all-module capture),
runtime control (port 47555), `viblog serve` UI, `review`, percentile
methodology — is viblog code, unchanged.

## 3. NidaqSource design (the core new code)

One `nidaqmx.Task` for all modules/channels (same-task channels share the
sample clock → simultaneous sampling across modules in one chassis):

```
task.ai_channels.add_ai_accel_chan(
    "cDAQ1Mod1/ai0", sensitivity=<mV/g from config>,
    units=AccelUnits.G, current_excit_val=0.002/0.004 per config, ...)
task.timing.cfg_samp_clk_timing(rate, sample_mode=CONTINUOUS,
                                samps_per_chan=<several seconds of buffer>)
reader = AnalogMultiChannelReader(task.in_stream)
```

Drain loop (poll model, matching viblog's runner cadence):
`task.in_stream.avail_samp_per_chan` → `reader.read_many_sample(preallocated,
number_of_samples_per_channel=avail)` → one **chunk** per module
(t_ns vector from t0 + counter/fs, counter ticks, values[n, ch]).

**Upstream change needed (small, phase 0):** viblog's runner currently
consumes per-sample `SweepData` objects and regroups them into arrays. At
2560 S/s that would work, but at 51.2 kS/s x 4 ch it's needless overhead —
and the DAQ read already produces arrays. Add a chunk-native path to viblog
(`ChunkSource` protocol or an optional `get_chunks()` on SweepSource; the
runner already operates on `(t0_ns, t_ns, ticks, values)` internally, so this
is mostly moving the existing `contiguous_runs` conversion into the MSCL
source). Contributed as a PR to hbk_viblog so both projects share one runner.

Timestamps: `t0_ns = time.time_ns()` captured when the task starts (refined
against the first read); sample k → `t0_ns + k * 1e9 / fs`. Health rows log
`(host_now - (t0 + n/fs))` so timebase-vs-host drift is visible.

Units: channels configured in **g** directly (DAQmx applies sensitivity), so
raw Parquet stays in g and the trigger threshold semantics match viblog
exactly. ±5 V input with a 10 V/g seismic accelerometer (PCB 393B05) means a
±0.5 g measurement range — fine for VC/footfall work, worth an explicit
config-time warning when |threshold| approaches the sensor's range.

Startup sensor check (record + monitor): brief DC-coupled read per IEPE
channel → bias voltage → classify healthy/short/open → session.json +
health surface; then reconfigure AC-coupled and start the real task.

## 4. Config sketch

```yaml
site: bench
mode: record | monitor
source: nidaq                    # viblog default remains the wireless source
nidaq:
  chassis: cDAQ1                 # name from NI MAX
  rate_hz: 2560                  # must be a native rate: 51200/n, n=1..31
  modules:
    - device: cDAQ1Mod1          # NI-9234
      channels:
        - physical: ai0
          sensor: {model: PCB 393B05, sn: "12345", sensitivity_mv_per_g: 10000}
          iepe: true
          coupling: ac
          orientation: "vertical, slab midbay"
# processing / trigger / control / ring: identical to viblog
```

## 5. What stays literally identical for the operator

`viblog serve` UI (health cards show IEPE/bias state instead of RSSI/battery),
`viblog ctl`, `viblog review`, session directory layout, metrics/trigger
semantics, resetting-interval statistics, profiles/reports (viblog phase 6,
shared). Training cost for switching between wireless and wired kits ≈ zero.
