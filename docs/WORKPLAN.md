# Work plan — nidaq-viblog

Status legend: ☐ not started · ◐ in progress · ✅ done
(Planned 2026-08-02 in the viblog session; coded in its own session.)

## Phase 0 — Bootstrap + upstream chunk path ✅ (coded 2026-08-02)

- ✅ Repo scaffold in `C:\Temp\nidaq`: pyproject (uv, Python ≥3.11 — matched to
  the on-machine viblog venv rather than 3.13), package `nidaq_viblog`, viblog
  as an **editable local path dependency** (`[tool.uv.sources] viblog = { path
  = "../mscl", editable = true }`; swap for the git source for off-machine repro)
- ◐ **Upstream PR to hbk_viblog** (chunk-native source path): **deferred, not
  required.** `NidaqSource` emits per-sample `SweepData` and viblog's existing
  runner does its own `contiguous_runs` regrouping — this works with *unmodified*
  viblog, and at the default 2560 S/s the per-sample overhead is negligible
  (proven: 7.5k sweeps/3 s, 0 gaps). Keep the chunk PR as a throughput
  optimization for sustained 51.2 kS/s × 4 ch runs (see ARCHITECTURE §3), not a
  blocker. Revisit in phase 6 if the drain benchmark (spike 03) shows the
  per-sample path is the bottleneck.
- ✅ `nidaqmx` dependency + driver check command: `nidaq-viblog devices`
  enumerates NI-DAQmx devices and prints the `python -m nidaqmx installdriver`
  hint when the runtime is absent (degrades cleanly — verified without a driver)
- ✅ Simulated end-to-end proof: **both** paths run through this repo's CLI —
  `--simulate` (viblog's own SimSource, the dependency-wiring proof) and
  `--sim-nidaq` (this project's `NidaqSource` + simulation backend). Automated
  in `tests/test_end_to_end.py`; full suite green (20 tests)

## Phase 1 — Hardware bench spike ☐ (needs cDAQ + NI-9234 + one IEPE sensor)

Mirrors viblog's `spike/` pattern: small scripts, each printing pass criteria,
findings recorded in HARDWARE_NOTES.

- ☐ 01: enumerate devices (`nidaqmx.system.System.local()`), module identity,
  verify chassis/module names vs NI MAX
- ☐ 02: rate ladder validation — request 2560/25600/51200 and off-ladder rates;
  confirm what DAQmx coerces (read back `task.timing.samp_clk_rate`)
- ☐ 03: drain benchmark — continuous accel task, poll `avail_samp_per_chan` +
  `read_many_sample` at ~100 ms cadence for 10 min at 51.2 kS/s × 4 ch;
  measure poll p95, buffer high-water, Parquet write cost; **verify zero
  overflow at max rate** (the wired analog of viblog's spike 03)
- ☐ 04: forced-overflow drill — stall reads deliberately; confirm error
  -200279 surfaces, task state afterward, and the recover-with-gap path
- ☐ 05: IEPE bias check — DC-coupled read per channel with sensor attached /
  detached / shorted; record bias voltages for the health classifier
- ☐ 06: TEDS probe — if sensors support TEDS, read sensitivity electronically
- ☐ 07: disconnect drill — pull USB mid-acquisition; document the exception,
  device re-enumeration time, task restart procedure
- ☐ Timing: log host-vs-timebase drift over an hour (expect ≤ ~50 ppm)

Exit: rates/throughput/overflow/IEPE behavior verified; NidaqSource design
assumptions confirmed or corrected in HARDWARE_NOTES.

## Phase 2 — NidaqSource + config (record mode) ◐ (code landed ahead of the hardware gate, sim-verified)

- ✅ Config schema (`source: nidaq`, chassis/modules/channels/sensor table) with
  validation (`config.py`): native-rate check (rejects off-ladder, names the
  coerced rate), sensitivity sanity, range-vs-threshold warning for 10 V/g
  sensors on ±5 V inputs, unique module indices
- ✅ `NidaqSource` (`nidaq_source.py`) implementing viblog's per-sample source
  protocol via an injectable `ReaderBackend` (`backends.py`: real `NidaqmxBackend`
  + hardware-free `SimBackend`): startup IEPE bias check → task build → drain →
  per-sample `SweepData`; sample index from the device's own cumulative counter
  (`total_acquired − avail`) → synthesized uint16 tick feeding the existing gap
  ledger; overflow → ledger gap + loud health flag (unit-tested deterministically)
- ✅ Device identity + sensor table + IEPE bias + requested/actual rate readback
  into session.json (verified in the sim run)
- ✅ `nidaq-viblog run` CLI (thin wrapper choosing source; `--simulate` /
  `--sim-nidaq` / real)
- ☐ **Bench validation (needs hardware): 30-min record session, tick ledger
  clean, `viblog review` renders.** This is the remaining phase-2 gate.
- **Field-usable wired logger pending only the bench validation above**

## Phase 3 — Monitor mode + trigger validation ☐

Expected ~free (all viblog code): monitor preset, metrics sink, trigger with
pre/post-roll event capture, runtime control. Work here is validation only:
- ☐ Bench drill: tap test → event captured across all modules; thresholds vs
  the ±0.5 g range of 10 V/g sensors documented
- ☐ Long soak (overnight): metrics-only monitor run, zero overflow, interval
  statistics reset on schedule

## Phase 4 — Live UI adaptation ☐

- ☐ Health cards: replace RSSI/battery fields with IEPE bias state, buffer
  fill %, drift; requires a small upstream hook for source-specific health
  fields (or a generic `extra` dict on health rows — preferred)
- ☐ Verify live raw + resetting-interval spectra in-browser against hardware

## Phase 5 — Profiles + reports ☐ (shared with viblog phase 6)

DG11 Ch. 6 / Ch. 3-4 profiles and matplotlib/DOCX reporting are planned as
viblog phase 6 and land there; this project inherits them through the
dependency. Only wired-specific report fields (sensor table, IEPE checks)
are added here.

## Phase 6 — Hardening ☐

- ☐ USB-disconnect auto-recovery (restart task, ledger gap, health event)
- ☐ Multi-day soak; disk budget check (51.2 kS/s × 4 ch ≈ 0.8 GB/h raw f32 —
  decide on float32 vs int32 counts, and whether software decimation to
  ~2.5 kS/s is wanted for long record-mode runs)
- ☐ Packaging + operator checklist (NI MAX naming, driver version pinning)

## Open questions for the owner (answer at coding-session kickoff)

1. **Chassis**: which cDAQ (USB 9171/9174/9178 vs Ethernet 9185/9188)? How many
   NI-9234 modules per kit?
2. **Sensor inventory**: PCB 393B05 (10 V/g, ±0.5 g) only, or mixed? TEDS-capable?
3. **Default rate**: 2560 S/s to match the NIH datasets, or higher? (Native
   ladder only; minimum native rate is ~1.65 kS/s — there is no 512 Hz on a
   9234 without software decimation.)
4. **Repo/remote**: GitHub repo name for this project; PR rights on hbk_viblog
   for the phase-0 upstream change.
5. Any need to run wireless + wired **simultaneously in one session** (two
   sources, one session dir)? Currently out of scope; say so if wanted, it
   affects the phase-0 runner change.
