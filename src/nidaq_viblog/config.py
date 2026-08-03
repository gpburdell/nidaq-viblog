"""Configuration for a wired NI DAQ session.

Two things come out of one YAML file:

* a viblog :class:`~viblog.config.SessionConfig` for everything the shared stack
  already understands (mode, processing, trigger, ring, control, poll, health) —
  loaded by viblog's own loader so its validation and defaults apply unchanged; and
* a :class:`NidaqConfig` for the NI-specific hardware table (chassis, rate,
  modules, per-channel sensor/IEPE/coupling) that only :class:`NidaqSource` reads.

The wired equivalent of SensorConnect's EEPROM (ARCHITECTURE.md §2) is this
YAML-owned sensor table: sensitivity, IEPE, coupling and orientation live here
because the module has no on-board sensor identity to read back (except TEDS,
probed in phase 1). Cross-cutting checks that need *both* halves — native-rate
coercion, and the ±0.5 g range of a 10 V/g sensor on a ±5 V input vs the trigger
threshold — live in :meth:`NidaqConfig.validate`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from viblog.config import NodeConfig, SessionConfig

from nidaq_viblog import rates

log = logging.getLogger(__name__)

# NI-9234 electrical facts used for config-time sanity checks (HARDWARE_NOTES,
# all (verify) on the bench). Input span is ±5 V; a sensor of S mV/g therefore
# saturates at (5000 / S) g.
NI9234_INPUT_VPK = 5.0


TEMPLATE = """\
# nidaq-viblog session configuration (wired NI DAQ / NI-9234)
site: bench                       # short site/job label; used in the session dir name
mode: record                      # record = full raw capture | monitor = metrics + triggered raw
output_dir: sessions
source: nidaq                     # selects the NI DAQ source (this project's default)

nidaq:
  chassis: cDAQ1                  # chassis name from NI MAX (informational / provenance)
  rate_hz: 2560                   # MUST be a native NI-9234 rate: 51200/n, n=1..31.
                                  # 2560 (n=20) matches the NIH reference datasets exactly.
  bias_check: true                # DC-coupled IEPE bias read at startup (health classifier)
  modules:
    - device: cDAQ1Mod1           # NI-9234 device name from NI MAX
      channels:
        - physical: ai0
          sensor: {model: PCB 393B05, sn: "12345", sensitivity_mv_per_g: 10000}
          iepe: true              # IEPE (ICP) excitation on
          excitation_a: 0.002     # excitation current, A (NI-9234 ~2 mA; (verify))
          coupling: ac            # ac (measurement) | dc (bias read)
          orientation: "vertical (Z), slab midbay"

raw:
  segment_seconds: 60             # one raw Parquet segment per this many seconds

processing:                       # windowed metrics (peak/RMS/1-3-octave); stored in SI
  enabled: true                   # forced on in monitor mode
  window_s: 1.0
  hp_hz: 0.5
  modalities: [accel]             # accel | vel | both
  vel_hp_hz: 1.0
  segment_seconds: 900

trigger:                          # triggered raw capture (evaluated per window)
  enabled: false                  # monitor mode defaults this to true
  threshold_g: 0.01               # window peak acceleration, g (runtime-changeable)
  channels: []                    # e.g. [ch1] — empty = any channel (OR logic)
  pre_roll_s: 10
  post_roll_s: 30
  hold_off_s: 5

ring_seconds: 120                 # RAM ring buffer per module (must cover pre_roll)

control:
  port: 47555                     # localhost control API for `viblog ctl` / web UI (0 = off)

poll_ms: 100                      # drain-loop cadence
health_interval_s: 5

# --- simulate mode (nidaq-viblog run --simulate) ----------------------------
# Uses viblog's built-in SimSource to prove the pipeline without hardware.
simulate:
  nodes:
    - address: 1
      sample_rate_hz: 2560
      n_channels: 1
      tones: [[12.5, 0.001], [50.0, 0.0005]]   # [freq_hz, peak_amplitude_g]
      gaps: []
      bursts: []
"""


@dataclass
class SensorSpec:
    model: str = ""
    sn: str = ""
    sensitivity_mv_per_g: float = 0.0

    @property
    def range_g(self) -> float:
        """Full-scale measurement range (g) for this sensor on a ±5 V input."""
        if self.sensitivity_mv_per_g <= 0:
            return float("inf")
        return NI9234_INPUT_VPK * 1000.0 / self.sensitivity_mv_per_g


@dataclass
class ChannelConfig:
    physical: str                       # e.g. "ai0"
    sensor: SensorSpec = field(default_factory=SensorSpec)
    iepe: bool = True
    excitation_a: float = 0.002
    coupling: str = "ac"                # ac | dc
    orientation: str = ""

    @property
    def excitation_source(self) -> str:
        return "INTERNAL" if self.iepe else "NONE"


@dataclass
class ModuleConfig:
    device: str                         # e.g. "cDAQ1Mod1"
    index: int                          # synthesized int "node address" for the ledger
    channels: list[ChannelConfig] = field(default_factory=list)

    def physical_channels(self) -> list[str]:
        return [f"{self.device}/{c.physical}" for c in self.channels]

    def channel_names(self) -> tuple[str, ...]:
        return tuple(c.physical for c in self.channels)

    def orientation_map(self) -> dict[str, str]:
        return {c.physical: c.orientation for c in self.channels if c.orientation}


@dataclass
class NidaqConfig:
    chassis: str = "cDAQ1"
    rate_hz: float = 2560.0
    bias_check: bool = True
    modules: list[ModuleConfig] = field(default_factory=list)

    @classmethod
    def from_yaml(cls, path: str | Path) -> "NidaqConfig":
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        nd = raw.get("nidaq") or {}
        modules: list[ModuleConfig] = []
        for i, m in enumerate(nd.get("modules") or [], start=1):
            channels = []
            for ch in m.get("channels") or []:
                s = ch.get("sensor") or {}
                channels.append(ChannelConfig(
                    physical=str(ch["physical"]),
                    sensor=SensorSpec(
                        model=str(s.get("model", "")),
                        sn=str(s.get("sn", "")),
                        sensitivity_mv_per_g=float(s.get("sensitivity_mv_per_g", 0.0)),
                    ),
                    iepe=bool(ch.get("iepe", True)),
                    excitation_a=float(ch.get("excitation_a", 0.002)),
                    coupling=str(ch.get("coupling", "ac")).lower(),
                    orientation=str(ch.get("orientation", "")),
                ))
            modules.append(ModuleConfig(
                device=str(m["device"]),
                index=int(m.get("index", i)),
                channels=channels,
            ))
        cfg = cls(
            chassis=str(nd.get("chassis", "cDAQ1")),
            rate_hz=float(nd.get("rate_hz", 2560)),
            bias_check=bool(nd.get("bias_check", True)),
            modules=modules,
        )
        return cfg

    def validate(self, session: SessionConfig) -> list[str]:
        """Raise on hard errors; return a list of soft warnings for the operator."""
        warnings: list[str] = []
        if not self.modules:
            raise ValueError("nidaq.modules is empty — declare at least one NI-9234 module")
        if any(not m.channels for m in self.modules):
            raise ValueError("every nidaq module must declare at least one channel")

        # Distinct module indices (they become node addresses in the ledger).
        indices = [m.index for m in self.modules]
        if len(set(indices)) != len(indices):
            raise ValueError(f"module indices must be unique, got {indices}")

        # Rate must be on the NI-9234 ladder, else the driver silently coerces it.
        if not rates.is_native(self.rate_hz):
            coerced = rates.nearest_native_rate(self.rate_hz)
            raise ValueError(
                f"rate_hz={self.rate_hz} is not a native NI-9234 rate (51200/n). "
                f"The driver would coerce it to {coerced:.4f} S/s. "
                f"Pick a native rate (e.g. 2560, 12800, 25600, 51200).")

        for m in self.modules:
            for c in m.channels:
                s = c.sensor
                if c.iepe and s.sensitivity_mv_per_g <= 0:
                    raise ValueError(
                        f"{m.device}/{c.physical}: IEPE channel needs a positive "
                        f"sensor.sensitivity_mv_per_g")
                if c.coupling not in ("ac", "dc"):
                    raise ValueError(
                        f"{m.device}/{c.physical}: coupling must be 'ac' or 'dc', "
                        f"got {c.coupling!r}")
                # Sensitivity sanity: seismic IEPE accelerometers are ~10..1000 mV/g.
                if s.sensitivity_mv_per_g and not (1.0 <= s.sensitivity_mv_per_g <= 100_000.0):
                    warnings.append(
                        f"{m.device}/{c.physical}: sensitivity {s.sensitivity_mv_per_g} mV/g "
                        f"looks out of range for an accelerometer — double-check units")

        # Range-vs-threshold: a 10 V/g sensor on ±5 V clips at ±0.5 g, so a trigger
        # threshold near that is unreachable (ARCHITECTURE.md §3, HARDWARE_NOTES).
        if session.trigger.enabled:
            thr = session.trigger.threshold_g
            for m in self.modules:
                for c in m.channels:
                    rng = c.sensor.range_g
                    if thr >= 0.8 * rng:
                        warnings.append(
                            f"{m.device}/{c.physical}: trigger threshold {thr} g is within "
                            f"80% of the sensor's ±{rng:.3g} g range ({c.sensor.sensitivity_mv_per_g} "
                            f"mV/g on ±{NI9234_INPUT_VPK} V) — exceedances may be unreachable "
                            f"or clipped")
        return warnings


def load(path: str | Path) -> tuple[SessionConfig, NidaqConfig]:
    """Load a wired session config: (viblog SessionConfig, NidaqConfig).

    The viblog loader owns the shared sections (and ignores the ``nidaq:`` key);
    NidaqConfig owns the hardware table. We then graft the module table onto the
    SessionConfig as synthesized nodes so orientation and the module→address map
    flow through session.json and the live UI unchanged.
    """
    session = SessionConfig.load(path)
    nidaq = NidaqConfig.from_yaml(path)
    warnings = nidaq.validate(session)
    for w in warnings:
        log.warning("config: %s", w)
    # Synthesized viblog nodes (address = module index) carry orientation forward.
    session.nodes = [
        NodeConfig(address=m.index, orientation=m.orientation_map())
        for m in nidaq.modules
    ]
    return session, nidaq
