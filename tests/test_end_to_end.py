"""End-to-end: NidaqSource driven by the real viblog Runner (no hardware).

This is the WORKPLAN phase-0 "simulated end-to-end proof" as an automated test:
the NI source, viblog's runner, ledger, sinks and Parquet output all exercised
together. Uses the simulation backend and a short real-time run.
"""

from pathlib import Path

from viblog.runner import Runner
from viblog.session import Session

from nidaq_viblog.config import load
from nidaq_viblog.nidaq_source import NidaqSource


def test_record_session_writes_clean_dataset(tmp_path):
    session_cfg, nidaq_cfg = load("bench.yaml")
    session_cfg.output_dir = str(tmp_path)

    source = NidaqSource.with_sim(nidaq_cfg)
    session = Session(session_cfg)
    runner = Runner(session_cfg, source, session, simulate=True)
    summary = runner.run(duration_s=0.6)

    node = summary["nodes"]["1"]
    assert node["sweeps"] > 0
    assert node["missing_sweeps"] == 0
    assert node["gap_events"] == 0
    assert node["rows_written"] == node["sweeps"]

    # raw Parquet + session.json landed on disk
    sess_dir = Path(session.dir)
    assert (sess_dir / "session.json").exists()
    assert list(sess_dir.glob("raw/**/*.parquet"))


def test_monitor_mode_runs_with_trigger(tmp_path):
    session_cfg, nidaq_cfg = load("bench.yaml")
    session_cfg.output_dir = str(tmp_path)
    session_cfg.mode = "monitor"
    session_cfg.validate()          # forces processing on, trigger default for monitor
    session_cfg.trigger.enabled = True
    session_cfg.trigger.threshold_g = 0.01

    source = NidaqSource.with_sim(nidaq_cfg)
    session = Session(session_cfg)
    runner = Runner(session_cfg, source, session, simulate=True)
    summary = runner.run(duration_s=0.6)
    assert summary["nodes"]["1"]["missing_sweeps"] == 0
