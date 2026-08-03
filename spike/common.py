"""Shared helpers for the phase-1 NI DAQ bench spike scripts.

Bench tools, mirroring viblog/spike: small, each prints its own PASS/FAIL
criteria, favor printing what the hardware actually reports over assuming API
shapes. These import ``nidaqmx`` directly (unlike the app, which hides it behind
a backend) — the whole point of a spike is to see the driver's real behavior.

Findings go back into docs/HARDWARE_NOTES.md.

Run any script with --help. Typical:
    python spike/03_drain_benchmark.py --device cDAQ1Mod1 --channels ai0,ai1 \
        --sensitivity 10000 --rate 51200 --minutes 10
"""

from __future__ import annotations

import argparse

from nidaq_viblog import rates


def add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--device", default="cDAQ1Mod1",
                        help="NI-9234 device name from NI MAX (default cDAQ1Mod1)")
    parser.add_argument("--channels", default="ai0",
                        help="comma-separated physical channels, e.g. ai0,ai1,ai2,ai3")
    parser.add_argument("--sensitivity", type=float, default=10000.0,
                        help="sensor sensitivity mV/g (PCB 393B05 = 10000)")
    parser.add_argument("--excitation", type=float, default=0.002,
                        help="IEPE excitation current, A (default 0.002)")
    parser.add_argument("--rate", type=float, default=2560.0,
                        help="requested sample rate S/s (default 2560)")


def physical_channels(args: argparse.Namespace) -> list[str]:
    return [f"{args.device}/{c.strip()}" for c in args.channels.split(",") if c.strip()]


def report_rate(requested: float, actual: float) -> None:
    native = rates.is_native(requested)
    nearest = rates.nearest_native_rate(requested)
    print(f"  requested={requested:.4f}  actual(readback)={actual:.4f}  "
          f"native={native}  nearest_ladder={nearest:.4f}")
    if abs(actual - requested) > 1e-3:
        print(f"  NOTE: driver coerced the rate by {actual - requested:+.4f} S/s")


def build_accel_task(nidaqmx, args, continuous: bool, buffer_seconds: float = 8.0):
    """Build (but do not start) a continuous/finite IEPE accel task in g."""
    from nidaqmx.constants import (AccelUnits, AcquisitionType, Coupling,
                                   ExcitationSource)

    task = nidaqmx.Task()
    for phys in physical_channels(args):
        ch = task.ai_channels.add_ai_accel_chan(
            phys, sensitivity=args.sensitivity, units=AccelUnits.G,
            current_excit_source=ExcitationSource.INTERNAL,
            current_excit_val=args.excitation)
        ch.ai_coupling = Coupling.AC
    if continuous:
        buf = max(int(buffer_seconds * args.rate), int(args.rate))
        task.timing.cfg_samp_clk_timing(
            args.rate, sample_mode=AcquisitionType.CONTINUOUS, samps_per_chan=buf)
        task.in_stream.input_buf_size = buf
    else:
        task.timing.cfg_samp_clk_timing(args.rate, samps_per_chan=int(args.rate))
    return task


def import_nidaqmx():
    try:
        import nidaqmx
        return nidaqmx
    except Exception as e:  # noqa: BLE001
        raise SystemExit(
            f"could not import nidaqmx ({e}).\n"
            f"Install the NI-DAQmx driver/runtime:  python -m nidaqmx installdriver")
