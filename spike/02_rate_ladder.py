"""Spike 02 — rate-ladder validation: what does the driver actually coerce to?

Requests a set of on- and off-ladder rates, reads back task.timing.samp_clk_rate,
and compares to rates.py's predicted NI-9234 ladder (51200/n).

PASS: native rates come back unchanged; off-ladder rates coerce to exactly the
divisor rates.py predicts (confirms the ladder model and the n range).
"""

from __future__ import annotations

import argparse

from common import add_common_args, build_accel_task, import_nidaqmx, report_rate

from nidaq_viblog import rates

# On-ladder (should be unchanged) and off-ladder (should be coerced).
TEST_RATES = [51200, 25600, 12800, 2560, 2048, 1651.6129,  # native
              2000, 5000, 512, 1024]                        # off-ladder


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    add_common_args(parser)
    args = parser.parse_args()
    nidaqmx = import_nidaqmx()

    print("NI-9234 predicted native ladder (51200/n, n=1..31):")
    print("  " + ", ".join(f"{r:.1f}" for r in rates.native_rates()))
    print()

    all_ok = True
    for req in TEST_RATES:
        args.rate = req
        try:
            task = build_accel_task(nidaqmx, args, continuous=True)
            actual = float(task.timing.samp_clk_rate)
            task.close()
        except Exception as e:  # noqa: BLE001
            print(f"{req:>10.1f}: ERROR {type(e).__name__}: {e}")
            all_ok = False
            continue
        report_rate(req, actual)
        predicted = req if rates.is_native(req) else rates.nearest_native_rate(req)
        if abs(actual - predicted) > 1e-2:
            print(f"  MISMATCH: predicted {predicted:.4f} but driver gave {actual:.4f}")
            all_ok = False

    print("\nPASS" if all_ok else "\nFAIL — update rates.py / HARDWARE_NOTES to match the driver")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
