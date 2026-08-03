"""NidaqSource — a wired NI DAQ source behind viblog's ``SweepSource`` protocol.

This is the one piece of genuinely new acquisition code (ARCHITECTURE.md §3).
Everything downstream — ledger, sinks, ring, DSP, trigger, live UI, review — is
viblog's and consumes the same ``(SweepData, DiagRecord)`` stream this produces.

Design in one paragraph: all configured channels sit in a single DAQmx task, so
they share the sample clock (simultaneous sampling across modules). Each poll we
read every available sample and turn it into one ``SweepData`` per module. The
per-module sample index comes straight from the device's own cumulative counter
via ``first_index = total_acquired - avail`` — so if the host buffer ever
overflows (DAQmx -200279, the wired analog of radio loss), the index jumps, the
synthesized uint16 tick jumps with it, and viblog's tick ledger records the gap
with no special-casing here. Timestamps are host-anchored: ``t0`` captured at
start (refined on the first read), sample ``k`` at ``t0 + k / fs``.
"""

from __future__ import annotations

import logging
import time

import numpy as np
from viblog.acquisition.types import DiagRecord, NodeInfo, SweepData

from nidaq_viblog.backends import ChannelPlan, Overflow, ReaderBackend
from nidaq_viblog.config import NidaqConfig

log = logging.getLogger(__name__)

_NS = 1_000_000_000

# IEPE bias-voltage classification band (HARDWARE_NOTES — (verify) on the bench,
# spike 05). Healthy sensors bias mid-supply; a short pulls to ~0 V, an open /
# unpowered / missing sensor floats near the excitation rail.
BIAS_SHORT_V = 1.0
BIAS_LOW_V = 7.0
BIAS_HIGH_V = 15.0


def classify_bias(v: float) -> str:
    """Map an IEPE bias voltage to a health state."""
    if v != v:  # NaN → IEPE not enabled on this channel
        return "n/a"
    if v < BIAS_SHORT_V:
        return "short"
    if v < BIAS_LOW_V:
        return "low"
    if v <= BIAS_HIGH_V:
        return "healthy"
    return "open"


class _ModuleView:
    """Maps one module's contiguous slice of the flat channel plan."""

    __slots__ = ("address", "device", "start", "stop", "channel_names")

    def __init__(self, address: int, device: str, start: int, stop: int,
                 channel_names: tuple[str, ...]) -> None:
        self.address = address
        self.device = device
        self.start = start
        self.stop = stop
        self.channel_names = channel_names


def channel_plan(config: NidaqConfig) -> ChannelPlan:
    """Flat, task-order channel plan across all modules (backend-agnostic)."""
    physical, sens, iepe, excit, coupling = [], [], [], [], []
    for m in config.modules:
        for c in m.channels:
            physical.append(f"{m.device}/{c.physical}")
            sens.append(c.sensor.sensitivity_mv_per_g)
            iepe.append(c.iepe)
            excit.append(c.excitation_a)
            coupling.append(c.coupling)
    return ChannelPlan(physical, sens, iepe, excit, coupling)


class NidaqSource:
    """SweepSource over an NI cDAQ chassis (one task, all modules/channels)."""

    def __init__(self, config: NidaqConfig, backend: ReaderBackend) -> None:
        self.config = config
        self.backend = backend
        self._buf: np.ndarray | None = None
        self._fs = config.rate_hz
        self._t0_ns = 0
        self._t0_set = False
        self._last_index = -1          # highest sample index emitted so far
        self._pending_diags: list[DiagRecord] = []
        self._overflow_events = 0

        # Per-module views into the flat channel plan (same task order).
        self._modules: list[_ModuleView] = []
        offset = 0
        for m in config.modules:
            k = len(m.channels)
            self._modules.append(_ModuleView(
                address=m.index, device=m.device, start=offset, stop=offset + k,
                channel_names=m.channel_names()))
            offset += k
        self.plan = channel_plan(config)

    # -- construction helpers ---------------------------------------------- #
    @classmethod
    def with_nidaqmx(cls, config: NidaqConfig, **backend_kwargs) -> "NidaqSource":
        """Build a source backed by a real DAQmx task (lazy-imports nidaqmx)."""
        from nidaq_viblog.backends import NidaqmxBackend
        backend = NidaqmxBackend(channel_plan(config), config.rate_hz, **backend_kwargs)
        return cls(config, backend)

    @classmethod
    def with_sim(cls, config: NidaqConfig, **backend_kwargs) -> "NidaqSource":
        """Build a source backed by the numpy simulator (no hardware/driver)."""
        from nidaq_viblog.backends import SimBackend
        backend = SimBackend(channel_plan(config), config.rate_hz, **backend_kwargs)
        return cls(config, backend)

    # -- lifecycle ---------------------------------------------------------- #
    def start(self) -> list[NodeInfo]:
        biases: list[float] = []
        if self.config.bias_check:
            try:
                biases = self.backend.bias_read()
            except Exception as e:  # noqa: BLE001 — a failed bias probe must not abort a run
                log.warning("IEPE bias check failed (%s) — continuing without it", e)
                biases = []

        info = self.backend.start()
        self._t0_ns = time.time_ns()
        self._t0_set = False
        self._fs = float(info.get("actual_rate_hz", self.config.rate_hz))
        if abs(self._fs - self.config.rate_hz) > 1e-6:
            log.warning("Requested %.4f S/s but the driver is running at %.4f S/s "
                        "(coerced to the NI-9234 ladder)", self.config.rate_hz, self._fs)
        identity = info.get("identity", {})

        nodes: list[NodeInfo] = []
        for mv in self._modules:
            ch_bias = biases[mv.start:mv.stop] if biases else []
            bias_doc = {
                name: {"volts": (round(v, 3) if v == v else None), "state": classify_bias(v)}
                for name, v in zip(mv.channel_names, ch_bias)
            }
            unhealthy = [n for n, d in bias_doc.items() if d["state"] not in ("healthy", "n/a")]
            if unhealthy:
                log.warning("Module %s (%s): IEPE bias suspect on %s — %s",
                            mv.address, mv.device, unhealthy,
                            {n: bias_doc[n] for n in unhealthy})
            snapshot = {
                "wired": True,
                "chassis": self.config.chassis,
                "device": mv.device,
                "identity": identity.get(mv.device, identity),
                "requested_rate_hz": self.config.rate_hz,
                "actual_rate_hz": self._fs,
                "sensors": {
                    c.physical: {
                        "model": c.sensor.model, "sn": c.sensor.sn,
                        "sensitivity_mv_per_g": c.sensor.sensitivity_mv_per_g,
                        "range_g": round(c.sensor.range_g, 4),
                        "iepe": c.iepe, "coupling": c.coupling,
                        "excitation_a": c.excitation_a,
                    }
                    for c in _module_channels(self.config, mv.device)
                },
                "iepe_bias": bias_doc,
            }
            nodes.append(NodeInfo(
                address=mv.address, sample_rate_hz=self._fs,
                n_channels=mv.stop - mv.start, channel_names=mv.channel_names,
                snapshot=snapshot))
            # Surface bias as a diagnostic record on the first drain (health strip).
            if bias_doc:
                self._pending_diags.append(DiagRecord(
                    t_ns=self._t0_ns, node=mv.address,
                    channels={f"bias_{n}_v": (d["volts"] if d["volts"] is not None else float("nan"))
                              for n, d in bias_doc.items()}))
        return nodes

    # -- draining ----------------------------------------------------------- #
    def get_sweeps(self) -> tuple[list[SweepData], list[DiagRecord]]:
        diags = self._drain_diags()
        avail = self.backend.avail()
        if avail <= 0:
            return [], diags
        total = self.backend.total_acquired()
        first_index = total - avail
        if first_index < 0:
            first_index = 0

        if self._buf is None or self._buf.shape[1] < avail:
            self._buf = np.empty((self.plan.n, avail), dtype=np.float64)

        try:
            self.backend.read_into(self._buf, avail)
        except Overflow as e:
            # Buffer overwrote unread samples. Don't emit anything this poll; the
            # next poll's first_index will have jumped, and the tick jump lands in
            # viblog's ledger as a gap. Loudly flag it as a health event.
            self._overflow_events += 1
            log.warning("DAQmx buffer OVERFLOW (#%d): %s — samples lost, gap will "
                        "be recorded", self._overflow_events, e)
            diags.append(DiagRecord(
                t_ns=time.time_ns(), node=self._modules[0].address,
                channels={"overflow_event": float(self._overflow_events)}))
            return [], diags

        if not self._t0_set:
            # Refine the anchor: the newest sample (index total-1) was acquired
            # ~now, so t0 = now - (total-1)/fs. Keeps absolute stamps within a
            # poll interval of the host clock (relative timing is exact regardless).
            self._t0_ns = time.time_ns() - round((total - 1) * _NS / self._fs)
            self._t0_set = True

        idx = np.arange(first_index, first_index + avail, dtype=np.int64)
        t_ns_vec = self._t0_ns + np.round(idx * (_NS / self._fs)).astype(np.int64)
        tick_vec = (idx & 0xFFFF).astype(np.int64)

        sweeps: list[SweepData] = []
        buf = self._buf
        for mv in self._modules:
            cols = range(mv.start, mv.stop)
            for k in range(avail):
                sweeps.append(SweepData(
                    t_ns=int(t_ns_vec[k]),
                    node=mv.address,
                    tick=int(tick_vec[k]),
                    values=tuple(float(buf[c, k]) for c in cols),
                ))
        self._last_index = first_index + avail - 1
        return sweeps, diags

    def buffered(self) -> int:
        try:
            return int(self.backend.avail())
        except Exception:  # noqa: BLE001
            return 0

    def stop(self) -> None:
        self.backend.close()

    # -- helpers ------------------------------------------------------------ #
    def _drain_diags(self) -> list[DiagRecord]:
        if not self._pending_diags:
            return []
        d, self._pending_diags = self._pending_diags, []
        return d


def _module_channels(config: NidaqConfig, device: str):
    for m in config.modules:
        if m.device == device:
            return m.channels
    return []
