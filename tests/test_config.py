"""Config loading + validation (native rate, range-vs-threshold, sensor sanity)."""

import textwrap

import pytest
from viblog.config import SessionConfig

from nidaq_viblog.config import NidaqConfig, load

BENCH = "bench.yaml"


def _write(tmp_path, body: str) -> str:
    p = tmp_path / "s.yaml"
    p.write_text(textwrap.dedent(body), encoding="utf-8")
    return str(p)


def test_bench_yaml_loads_both_halves():
    session, nidaq = load(BENCH)
    assert isinstance(session, SessionConfig)
    assert isinstance(nidaq, NidaqConfig)
    assert nidaq.rate_hz == 2560
    # module table grafted onto the viblog config as synthesized nodes (orientation)
    assert session.nodes[0].address == nidaq.modules[0].index
    assert session.nodes[0].orientation["ai0"].startswith("vertical")


def test_sensor_range_computed_from_sensitivity():
    _, nidaq = load(BENCH)
    ch = nidaq.modules[0].channels[0]
    assert ch.sensor.range_g == pytest.approx(0.5)  # 5000 mV / 10000 mV/g


def test_off_ladder_rate_is_rejected(tmp_path):
    cfg = _write(tmp_path, """
        site: t
        nidaq:
          rate_hz: 2000
          modules:
            - device: cDAQ1Mod1
              channels:
                - {physical: ai0, sensor: {sensitivity_mv_per_g: 100}}
    """)
    with pytest.raises(ValueError, match="not a native NI-9234 rate"):
        load(cfg)


def test_iepe_channel_needs_sensitivity(tmp_path):
    cfg = _write(tmp_path, """
        site: t
        nidaq:
          rate_hz: 2560
          modules:
            - device: cDAQ1Mod1
              channels:
                - {physical: ai0, iepe: true}
    """)
    with pytest.raises(ValueError, match="sensitivity"):
        load(cfg)


def test_threshold_near_range_warns():
    # 10 V/g sensor → ±0.5 g; a 0.45 g trigger threshold is within 80% of range.
    session = SessionConfig()
    session.trigger.enabled = True
    session.trigger.threshold_g = 0.45
    _, nidaq = load(BENCH)
    warnings = nidaq.validate(session)
    assert any("range" in w for w in warnings), warnings


def test_module_indices_must_be_unique(tmp_path):
    cfg = _write(tmp_path, """
        site: t
        nidaq:
          rate_hz: 2560
          modules:
            - device: cDAQ1Mod1
              index: 5
              channels: [{physical: ai0, sensor: {sensitivity_mv_per_g: 100}}]
            - device: cDAQ1Mod2
              index: 5
              channels: [{physical: ai0, sensor: {sensitivity_mv_per_g: 100}}]
    """)
    with pytest.raises(ValueError, match="unique"):
        load(cfg)
