"""Spike 04 — forced-overflow drill: make -200279 happen on purpose.

Deliberately stalls the drain loop so the host buffer overwrites unread samples,
then confirms the exact error code, the task state afterward, and whether reads
resume (informs NidaqSource's recovery path and the exact code in backends.py).

PASS: an overflow error surfaces with a known code (expected -200279); we record
the code, whether the task is still running, and whether a subsequent read
succeeds or the task must be stopped/restarted.
"""

from __future__ import annotations

import argparse
import time

import numpy as np
from common import add_common_args, build_accel_task, import_nidaqmx, physical_channels


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    add_common_args(parser)
    parser.add_argument("--stall-s", type=float, default=None,
                        help="seconds to stall (default: buffer + 2 s, guarantees overflow)")
    args = parser.parse_args()
    nidaqmx = import_nidaqmx()
    from nidaqmx.stream_readers import AnalogMultiChannelReader

    chans = physical_channels(args)
    task = build_accel_task(nidaqmx, args, continuous=True, buffer_seconds=4.0)
    actual = float(task.timing.samp_clk_rate)
    reader = AnalogMultiChannelReader(task.in_stream)
    stall = args.stall_s if args.stall_s is not None else 4.0 + 2.0

    task.start()
    print(f"Stalling {stall:.1f}s at {actual:.1f} S/s to force an overflow ...")
    time.sleep(stall)

    avail = int(task.in_stream.avail_samp_per_chan)
    total = int(task.in_stream.total_samp_per_chan_acquired)
    print(f"after stall: avail={avail} total_acquired={total}")

    code = None
    try:
        buf = np.empty((len(chans), max(avail, 1)), dtype=np.float64)
        reader.read_many_sample(buf, number_of_samples_per_channel=max(avail, 1))
        print("read succeeded (no overflow?) — increase --stall-s")
    except nidaqmx.errors.DaqReadError as e:  # type: ignore[attr-defined]
        code = e.error_code
        print(f"OVERFLOW error_code={code}  ({e})")

    # Task state + recovery probe
    try:
        avail2 = int(task.in_stream.avail_samp_per_chan)
        total2 = int(task.in_stream.total_samp_per_chan_acquired)
        print(f"post-error: avail={avail2} total_acquired={total2} "
              f"(counter still advancing = {total2 > total})")
        buf = np.empty((len(chans), max(avail2, 1)), dtype=np.float64)
        reader.read_many_sample(buf, number_of_samples_per_channel=max(avail2, 1))
        print("recovery read SUCCEEDED without restart — in-place resync works")
    except Exception as e:  # noqa: BLE001
        print(f"recovery read failed ({e}) — NidaqSource must stop/restart the task")
    finally:
        task.stop(); task.close()

    print(f"\nRecord in HARDWARE_NOTES: overflow code={code} "
          f"(backends.DAQMX_OVERWRITE_ERR is set to -200279).")
    print("PASS" if code is not None else "FAIL — no overflow produced; raise --stall-s")
    return 0 if code is not None else 1


if __name__ == "__main__":
    raise SystemExit(main())
