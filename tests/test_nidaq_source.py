"""NidaqSource drain logic: ticks, module splitting, overflow → ledger gap."""

from viblog.acquisition.ledger import TickLedger

from nidaq_viblog.backends import SimBackend
from nidaq_viblog.nidaq_source import NidaqSource, channel_plan, classify_bias

from helpers import FakeClock, make_config


def _source(config, clock, **backend_kw):
    backend = SimBackend(channel_plan(config), config.rate_hz,
                         time_fn=clock, **backend_kw)
    return NidaqSource(config, backend), backend


def test_start_reports_one_node_per_module_with_bias():
    cfg = make_config(modules=[("cDAQ1Mod1", ["ai0", "ai1"]), ("cDAQ1Mod2", ["ai0"])])
    src, _ = _source(cfg, FakeClock(0.0), bias_v=[9.5, 9.5, 9.5])
    nodes = src.start()
    assert [n.address for n in nodes] == [1, 2]
    assert nodes[0].n_channels == 2 and nodes[1].n_channels == 1
    assert nodes[0].snapshot["iepe_bias"]["ai0"]["state"] == "healthy"
    # bias is queued as a diagnostic record for the health strip
    _, diags = src.get_sweeps()
    assert any("bias_ai0_v" in d.channels for d in diags)


def test_ticks_are_contiguous_and_split_per_module():
    cfg = make_config(rate_hz=1000.0,
                      modules=[("cDAQ1Mod1", ["ai0"]), ("cDAQ1Mod2", ["ai0", "ai1"])])
    clock = FakeClock(0.0)
    src, _ = _source(cfg, clock)
    src.start()
    clock.t = 0.05  # 50 samples/ch produced
    sweeps, _ = src.get_sweeps()
    by_node = {1: [], 2: []}
    for s in sweeps:
        by_node[s.node].append(s)
    assert len(by_node[1]) == 50 and len(by_node[2]) == 50
    assert [s.tick for s in by_node[1]] == list(range(50))         # contiguous ticks
    assert len(by_node[1][0].values) == 1                          # module 1: 1 channel
    assert len(by_node[2][0].values) == 2                          # module 2: 2 channels


def test_tick_wraps_at_uint16():
    cfg = make_config(rate_hz=1000.0)
    clock = FakeClock(0.0)
    # capacity big enough to hold >65536 samples so nothing is dropped
    src, _ = _source(cfg, clock, capacity_samples=200_000)
    src.start()
    clock.t = 66.0  # 66000 samples > 65536
    sweeps, _ = src.get_sweeps()
    ticks = [s.tick for s in sweeps]
    assert max(ticks) < 65536
    assert 0 in ticks[65536 - 1:]  # wrapped back through zero


def test_overflow_becomes_a_ledger_gap():
    cfg = make_config(rate_hz=1000.0)
    clock = FakeClock(0.0)
    src, backend = _source(cfg, clock, capacity_samples=100)
    src.start()
    ledger = TickLedger()

    def feed(sweeps):
        for s in sweeps:
            ledger.update(s.node, s.tick, s.t_ns)

    clock.t = 0.05                       # 50 samples
    sweeps, _ = src.get_sweeps()
    feed(sweeps)
    assert len(sweeps) == 50

    clock.t = 0.16                       # 160 produced, backlog 110 > capacity 100
    sweeps, diags = src.get_sweeps()
    assert sweeps == []                  # overflow poll emits no data ...
    assert any("overflow_event" in d.channels for d in diags)  # ... but a health flag

    # recovery poll (clock unchanged): reads from the jumped index
    sweeps, _ = src.get_sweeps()
    feed(sweeps)
    assert sweeps and sweeps[0].tick == 60      # 10 samples (50..59) were lost

    c = ledger.nodes[1]
    assert c.gap_events == 1
    assert c.missing_sweeps == 10


def test_classify_bias():
    assert classify_bias(9.5) == "healthy"
    assert classify_bias(0.2) == "short"
    assert classify_bias(3.0) == "low"
    assert classify_bias(24.0) == "open"
    assert classify_bias(float("nan")) == "n/a"


def test_actual_rate_readback_flows_into_nodeinfo():
    cfg = make_config(rate_hz=2560.0)
    src, _ = _source(cfg, FakeClock(0.0))
    nodes = src.start()
    assert nodes[0].sample_rate_hz == 2560.0
    assert nodes[0].snapshot["actual_rate_hz"] == 2560.0
