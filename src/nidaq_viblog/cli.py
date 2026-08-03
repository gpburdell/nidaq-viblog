"""nidaq-viblog CLI — the thin wrapper that chooses the NI DAQ source.

Everything after acquisition is viblog's and works on the session directory
unchanged, so the live UI, runtime control, and review are viblog's own
commands (they are source-agnostic):

    viblog serve  --root sessions          # live web UI + session browser
    viblog ctl    status | set ...         # runtime control of a running session
    viblog review [session]                # interactive HTML review

This CLI provides only what is NI-specific:

    nidaq-viblog run --config bench.yaml               record from the chassis
    nidaq-viblog run --config bench.yaml --simulate    prove the pipeline (viblog SimSource)
    nidaq-viblog run --config bench.yaml --sim-nidaq    exercise NidaqSource w/o hardware
    nidaq-viblog init-config bench.yaml                write a commented config template
    nidaq-viblog devices                               enumerate NI-DAQmx devices
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from nidaq_viblog import __version__


def _cmd_run(args: argparse.Namespace) -> int:
    from viblog.runner import Runner
    from viblog.session import Session

    from nidaq_viblog.config import load

    if not Path(args.config).exists():
        print(f"error: config file not found: {args.config}\n"
              f"Create one with:  nidaq-viblog init-config {args.config}", file=sys.stderr)
        return 2

    session_cfg, nidaq_cfg = load(args.config)

    simulate = bool(args.simulate or args.sim_nidaq)
    if args.simulate:
        # Dependency-wiring proof (WORKPLAN phase 0): viblog's own SimSource,
        # driven entirely through this repo's CLI.
        from viblog.acquisition.sim import SimNodeSpec, SimSource
        specs = [SimNodeSpec(address=n.address, sample_rate_hz=n.sample_rate_hz,
                             n_channels=n.n_channels, tones=n.tones or [(12.5, 0.001)],
                             gaps=n.gaps, bursts=n.bursts)
                 for n in session_cfg.sim_nodes]
        if not specs:
            specs = [SimNodeSpec(address=nidaq_cfg.modules[0].index,
                                 sample_rate_hz=nidaq_cfg.rate_hz, n_channels=1)]
        source = SimSource(specs)
    elif args.sim_nidaq:
        # Exercise THIS project's NidaqSource end-to-end without a chassis.
        from nidaq_viblog.nidaq_source import NidaqSource
        source = NidaqSource.with_sim(nidaq_cfg)
    else:
        from nidaq_viblog.nidaq_source import NidaqSource
        source = NidaqSource.with_nidaqmx(nidaq_cfg)

    session = Session(session_cfg)
    print(f"Session: {session.dir}")
    runner = Runner(session_cfg, source, session, simulate=simulate)
    summary = runner.run(duration_s=args.duration)
    print("Summary:")
    for addr, s in summary.get("nodes", {}).items():
        print(f"  module {addr}: {s['sweeps']} sweeps, {s['rows_written']} rows written, "
              f"{s['missing_sweeps']} missing ({s['gap_events']} gap events), "
              f"{s['late_or_dup']} late/dup")
    return 0


def _cmd_init_config(args: argparse.Namespace) -> int:
    from nidaq_viblog.config import TEMPLATE
    path = Path(args.path)
    if path.exists() and not args.force:
        print(f"error: {path} exists (use --force to overwrite)", file=sys.stderr)
        return 2
    path.write_text(TEMPLATE, encoding="utf-8")
    print(f"Wrote {path}")
    return 0


def _cmd_devices(args: argparse.Namespace) -> int:
    try:
        import nidaqmx.system
    except Exception as e:  # noqa: BLE001
        print(f"error: could not import nidaqmx ({e}).\n"
              f"Install the NI-DAQmx driver/runtime:  python -m nidaqmx installdriver",
              file=sys.stderr)
        return 2
    try:
        system = nidaqmx.system.System.local()
        devices = list(system.devices)
    except Exception as e:  # noqa: BLE001
        print(f"error: NI-DAQmx driver present but device enumeration failed ({e}).\n"
              f"Is the runtime installed and a chassis connected?", file=sys.stderr)
        return 2
    if not devices:
        print("No NI-DAQmx devices found. Check the chassis connection and NI MAX.")
        return 1
    print(f"NI-DAQmx driver {system.driver_version}. Devices:")
    for d in devices:
        try:
            ai = [c.name for c in d.ai_physical_chans]
        except Exception:  # noqa: BLE001
            ai = []
        print(f"  {d.name:12s} {d.product_type:18s} sn={d.dev_serial_num}  "
              f"ai_channels={len(ai)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nidaq-viblog", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version=f"nidaq-viblog {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="record/monitor a session from the NI chassis")
    p_run.add_argument("--config", required=True, help="session YAML (see: nidaq-viblog init-config)")
    g = p_run.add_mutually_exclusive_group()
    g.add_argument("--simulate", action="store_true",
                   help="use viblog's SimSource (dependency-wiring proof, no hardware)")
    g.add_argument("--sim-nidaq", action="store_true", dest="sim_nidaq",
                   help="use NidaqSource with the simulation backend (no hardware)")
    p_run.add_argument("--duration", type=float, default=None,
                       help="seconds (default: until Ctrl+C)")
    p_run.set_defaults(func=_cmd_run)

    p_init = sub.add_parser("init-config", help="write a config template")
    p_init.add_argument("path")
    p_init.add_argument("--force", action="store_true")
    p_init.set_defaults(func=_cmd_init_config)

    p_dev = sub.add_parser("devices", help="enumerate NI-DAQmx devices (needs the driver)")
    p_dev.set_defaults(func=_cmd_devices)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
