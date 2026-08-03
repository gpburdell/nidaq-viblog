"""Spike 05 — IEPE bias check: DC-coupled bias voltage per channel.

Reads the steady DC bias on each IEPE channel with excitation on. Run it with the
sensor attached, detached, and shorted to record the three bias signatures the
health classifier keys on (nidaq_source.classify_bias thresholds).

PASS: attached sensors read a healthy mid-supply bias (~8-12 V expected); the
detached/shorted readings differ clearly enough to classify. Record all three in
HARDWARE_NOTES and tune BIAS_* thresholds in nidaq_source.py if needed.
"""

from __future__ import annotations

import argparse

import numpy as np
from common import add_common_args, import_nidaqmx, physical_channels

from nidaq_viblog.nidaq_source import classify_bias


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    add_common_args(parser)
    parser.add_argument("--seconds", type=float, default=0.5)
    args = parser.parse_args()
    nidaqmx = import_nidaqmx()
    from nidaqmx.constants import (Coupling, ExcitationSource,
                                   TerminalConfiguration)

    chans = physical_channels(args)
    with nidaqmx.Task() as task:
        for phys in chans:
            ch = task.ai_channels.add_ai_voltage_chan(
                phys, terminal_config=TerminalConfiguration.PSEUDO_DIFF,
                min_val=-30.0, max_val=30.0)
            ch.ai_coupling = Coupling.DC
            ch.ai_excit_src = ExcitationSource.INTERNAL
            ch.ai_excit_val = args.excitation
        n = max(1, int(args.seconds * args.rate))
        task.timing.cfg_samp_clk_timing(args.rate, samps_per_chan=n)
        data = np.asarray(task.read(number_of_samples_per_channel=n))
        if data.ndim == 1:
            data = data[np.newaxis, :]

    print(f"IEPE bias ({args.excitation * 1000:.1f} mA excitation, DC-coupled):")
    for phys, col in zip(chans, data):
        v = float(col.mean())
        print(f"  {phys:20s} bias={v:7.3f} V  -> {classify_bias(v)}")
    print("\nRun attached / detached / shorted and compare the states above.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
