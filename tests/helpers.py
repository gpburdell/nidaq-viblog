"""Shared test helpers (imported by name; tests/ is on sys.path under pytest)."""

from nidaq_viblog.config import (ChannelConfig, ModuleConfig, NidaqConfig,
                                 SensorSpec)


class FakeClock:
    """Manually-advanced monotonic clock for deterministic drain tests."""

    def __init__(self, t: float = 0.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def make_config(rate_hz: float = 2560.0, modules=None, bias_check: bool = True) -> NidaqConfig:
    """Build a NidaqConfig without YAML. ``modules`` = list of (device, [ai names])."""
    if modules is None:
        modules = [("cDAQ1Mod1", ["ai0"])]
    mods = []
    for i, (device, chans) in enumerate(modules, start=1):
        mods.append(ModuleConfig(
            device=device, index=i,
            channels=[ChannelConfig(
                physical=ai,
                sensor=SensorSpec(model="PCB 393B05", sn="x",
                                  sensitivity_mv_per_g=10000.0),
                iepe=True, excitation_a=0.002, coupling="ac",
                orientation=f"{device}/{ai}")
                for ai in chans],
        ))
    return NidaqConfig(chassis="cDAQ1", rate_hz=rate_hz, bias_check=bias_check,
                       modules=mods)
