"""Spike 03 — drain benchmark: can single-threaded Python keep up at max rate?

The wired analog of viblog's spike 03. Runs a continuous accel task, polls
avail_samp_per_chan + read_many_sample into a preallocated array at ~100 ms
cadence, and measures poll latency, buffer high-water, and read throughput.

PASS: zero -200279 overflow errors over the whole run at 51.2 kS/s x N ch;
poll p95 comfortably under the poll interval; buffer high-water well under the
configured buffer.
"""

from __future__ import annotations

import argparse
import time

import numpy as np
from common import (add_common_args, build_accel_task, import_nidaqmx,
                    physical_channels, report_rate)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    add_common_args(parser)
    parser.add_argument("--minutes", type=float, default=10.0, help="run length (default 10)")
    parser.add_argument("--poll-ms", type=float, default=100.0)
    args = parser.parse_args()
    nidaqmx = import_nidaqmx()
    from nidaqmx.stream_readers import AnalogMultiChannelReader

    chans = physical_channels(args)
    task = build_accel_task(nidaqmx, args, continuous=True)
    actual = float(task.timing.samp_clk_rate)
    report_rate(args.rate, actual)
    reader = AnalogMultiChannelReader(task.in_stream)

    buf = np.empty((len(chans), int(actual * 2)), dtype=np.float64)
    poll_times: list[float] = []
    high_water = 0
    overflows = 0
    total = 0
    t_end = time.monotonic() + args.minutes * 60
    poll_s = args.poll_ms / 1000.0

    task.start()
    t0 = time.time_ns()
    print(f"Running {args.minutes:.1f} min at {actual:.1f} S/s x {len(chans)} ch ...")
    try:
        while time.monotonic() < t_end:
            tick = time.perf_counter()
            avail = int(task.in_stream.avail_samp_per_chan)
            high_water = max(high_water, avail)
            if avail > 0:
                if avail > buf.shape[1]:
                    buf = np.empty((len(chans), avail), dtype=np.float64)
                try:
                    reader.read_many_sample(buf, number_of_samples_per_channel=avail)
                    total += avail
                except nidaqmx.errors.DaqReadError as e:  # type: ignore[attr-defined]
                    overflows += 1
                    print(f"  OVERFLOW ({e.error_code}): {e}")
            poll_times.append(time.perf_counter() - tick)
            time.sleep(poll_s)
    except KeyboardInterrupt:
        print("interrupted")
    finally:
        task.stop(); task.close()

    elapsed = (time.time_ns() - t0) / 1e9
    p = np.percentile(np.array(poll_times) * 1e3, [50, 95, 99]) if poll_times else [0, 0, 0]
    expected = actual * elapsed
    print(f"\nsamples read     : {total}  (expected ~{expected:.0f}, "
          f"{100 * total / expected:.2f}%)")
    print(f"poll latency ms  : p50={p[0]:.2f} p95={p[1]:.2f} p99={p[2]:.2f}")
    print(f"buffer high-water: {high_water} samples/ch "
          f"({high_water / actual:.2f} s of {args.rate * 8:.0f}-sample buffer)")
    print(f"overflow errors  : {overflows}")
    ok = overflows == 0 and total >= 0.999 * expected
    print("PASS" if ok else "FAIL — see overflow/throughput above")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
