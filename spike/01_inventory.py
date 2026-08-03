"""Spike 01 — enumerate NI-DAQmx devices and confirm module identity vs NI MAX.

PASS: every expected NI-9234 module shows up with product_type, serial, and 4 AI
channels; chassis/module names match the config you plan to run.
"""

from __future__ import annotations

from common import import_nidaqmx


def main() -> int:
    nidaqmx = import_nidaqmx()
    import nidaqmx.system

    system = nidaqmx.system.System.local()
    print(f"NI-DAQmx driver version: {system.driver_version}")
    devices = list(system.devices)
    if not devices:
        print("FAIL: no devices found. Check the chassis connection and NI MAX.")
        return 1

    for d in devices:
        try:
            ai = [c.name for c in d.ai_physical_chans]
        except Exception as e:  # noqa: BLE001
            ai = [f"<err: {e}>"]
        print(f"\n{d.name}")
        print(f"  product_type : {d.product_type}")
        print(f"  serial       : {d.dev_serial_num}")
        print(f"  ai channels  : {len(ai)}  {ai}")
        try:
            print(f"  terminals    : {d.terminals[:4]} ...")
        except Exception:  # noqa: BLE001
            pass
    print("\nPASS if the NI-9234 module(s) above match your config's device names.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
