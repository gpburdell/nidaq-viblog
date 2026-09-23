#!/usr/bin/env python
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "numpy>=2.0",
#   "scipy>=1.11",
#   "pyqtgraph>=0.13",
#   "PyQt6>=6.6",
#   "nidaqmx>=1.0",
# ]
# ///
"""Realtime vibration viewer for sensitive-space presentations (NI-9234).

Standalone demo tool — deliberately independent of the nidaq-viblog logging
pipeline (no viblog import, no session directory, nothing written to disk
except screenshots). It opens its own DAQmx task, shows a ~5 s rolling time
view of three channels plus a live spectrum, and reports everything as
**velocity in micro-inches per second (uin/s)** so the acceleration and laser
channels are directly comparable against the ASHRAE/IEST VC criteria used for
sensitive spaces.

Default hookup (matches the bench as wired 2026-09-22):

    cDAQ1Mod1/ai0   laser vibrometer   12.5 mV/(mm/s), IEPE OFF
    cDAQ1Mod1/ai1   accelerometer      1000 mV/g, IEPE 2 mA
    cDAQ1Mod1/ai2   accelerometer      1000 mV/g, IEPE 2 mA

Signal path
    laser  : DAQmx velocity channel (m/s)  -> HP -> x 3.937e7         -> uin/s
    accel  : DAQmx accel channel (g) -> x g0 -> HP -> integrate -> HP -> uin/s

Both HP stages are 4th-order Butterworth at --hp (default 1 Hz) run with
persistent filter state, so the stream stays continuous across blocks and the
integrator cannot walk away. In --sim the accel and laser channels carry the
*same* underlying velocity, so the traces lying on top of each other is a live
proof that the integration chain is right.

Usage (no project env needed; uv reads the inline dependencies above):

    uv run --no-project viewer/vc_viewer.py --sim          # rehearse, no hardware
    uv run --no-project viewer/vc_viewer.py                # real chassis
    uv run --no-project viewer/vc_viewer.py --selftest     # validate the DSP chain

Keys:  space pause | v PSD<->1/3-octave | p peak-hold | c common/per-channel
       y-scale | a rescale now | s screenshot PNG | q quit
"""

from __future__ import annotations

import argparse
import math
import sys
import time
from dataclasses import dataclass

import numpy as np
from scipy import signal

# --------------------------------------------------------------------------- #
# Units & criteria
# --------------------------------------------------------------------------- #
UIN_PER_M = 39_370_078.740157480        # 1 m = 3.937007874e7 micro-inch (1 in = 25.4 mm)
G0_M_S2 = 9.80665                       # standard gravity
NI9234_BASE_HZ = 51200.0                # native rates are 51200/n, n = 1..31
NI9234_INPUT_VPK = 5.0                  # +/-5 V input span

# ASHRAE/IEST generic vibration criteria, 1/3-octave RMS velocity, uin/s.
# Applied over the 8-80 Hz bands (the flat-velocity part of each curve).
VC_CRITERIA: list[tuple[str, float]] = [
    ("ISO Workshop", 32000.0),
    ("ISO Office", 16000.0),
    ("ISO Residential (day)", 8000.0),
    ("ISO Op. theatre", 4000.0),
    ("VC-A", 2000.0),
    ("VC-B", 1000.0),
    ("VC-C", 500.0),
    ("VC-D", 250.0),
    ("VC-E", 125.0),
]
VC_BAND_LO_HZ, VC_BAND_HI_HZ = 8.0, 80.0

# ANSI S1.11 preferred 1/3-octave centers, 1 .. 100 Hz (the VC range).
THIRD_OCTAVE_CENTERS = np.array(
    [1.0, 1.25, 1.6, 2.0, 2.5, 3.15, 4.0, 5.0, 6.3, 8.0, 10.0, 12.5,
     16.0, 20.0, 25.0, 31.5, 40.0, 50.0, 63.0, 80.0, 100.0])

TRACE_COLORS = ["#39ff88", "#4da6ff", "#ffb340", "#ff5c8a"]


def is_native_rate(rate_hz: float) -> bool:
    n = NI9234_BASE_HZ / rate_hz
    return abs(n - round(n)) < 1e-6 and 1 <= round(n) <= 31


def nearest_native_rate(rate_hz: float) -> float:
    n = min(max(round(NI9234_BASE_HZ / rate_hz), 1), 31)
    return NI9234_BASE_HZ / n


def vc_class(centers: np.ndarray, band_rms: np.ndarray) -> tuple[str, float]:
    """Strictest VC criterion met by the 8-80 Hz 1/3-octave bands."""
    m = (centers >= VC_BAND_LO_HZ) & (centers <= VC_BAND_HI_HZ) & np.isfinite(band_rms)
    if not m.any():
        return "--", float("nan")
    peak = float(np.max(band_rms[m]))
    for label, limit in reversed(VC_CRITERIA):      # strictest first
        if peak <= limit:
            return label, peak
    return "above ISO Workshop", peak


# --------------------------------------------------------------------------- #
# Channel description
# --------------------------------------------------------------------------- #
@dataclass
class ChannelSpec:
    physical: str                    # "cDAQ1Mod1/ai0"
    kind: str                        # "velocity" | "accel"
    sensitivity: float               # mV/(mm/s) for velocity, mV/g for accel
    iepe: bool = True
    excitation_a: float = 0.002
    coupling: str = "ac"
    label: str = ""

    @property
    def units(self) -> str:
        return "mV/(mm/s)" if self.kind == "velocity" else "mV/g"

    @property
    def native_unit(self) -> str:
        return "m/s" if self.kind == "velocity" else "g"

    @property
    def full_scale(self) -> float:
        """Clip level in native units on a +/-5 V input."""
        if self.sensitivity <= 0:
            return float("inf")
        if self.kind == "velocity":
            # mV per (mm/s) is numerically V per (m/s): 12.5 -> +/-0.4 m/s.
            return NI9234_INPUT_VPK / self.sensitivity
        return NI9234_INPUT_VPK * 1000.0 / self.sensitivity

    def describe(self) -> str:
        excit = f"IEPE {self.excitation_a * 1000:.1f} mA" if self.iepe else "IEPE off"
        return (f"{self.physical}  {self.kind:8s} {self.sensitivity:g} {self.units}  "
                f"{excit}, {self.coupling.upper()}-coupled, "
                f"clip +/-{self.full_scale:.4g} {self.native_unit}")


# --------------------------------------------------------------------------- #
# Acquisition
# --------------------------------------------------------------------------- #
class NidaqmxStream:
    """One continuous DAQmx task carrying the laser and the accelerometers.

    Reads whatever is available each poll; a host-buffer overflow is counted and
    shown on the status line rather than killing the demo.
    """

    def __init__(self, specs: list[ChannelSpec], rate_hz: float,
                 buffer_seconds: float = 4.0) -> None:
        self.specs = specs
        self.rate_hz = rate_hz
        self.actual_rate_hz = rate_hz
        self.buffer_seconds = buffer_seconds
        self.overflows = 0
        self.notes: list[str] = []
        self._task = None
        self._reader = None
        self._buf = np.empty((len(specs), 1), dtype=np.float64)

    def start(self) -> None:
        import nidaqmx
        from nidaqmx.constants import (AccelUnits, AcquisitionType, Coupling,
                                       ExcitationSource,
                                       VelocityIEPESensorSensitivityUnits,
                                       VelocityUnits)
        from nidaqmx.stream_readers import AnalogMultiChannelReader

        task = nidaqmx.Task()
        for spec in self.specs:
            src = ExcitationSource.INTERNAL if spec.iepe else ExcitationSource.NONE
            val = spec.excitation_a if spec.iepe else 0.0
            fs = spec.full_scale
            try:
                ch = self._add_channel(task, spec, src, val, fs, AccelUnits,
                                       VelocityUnits, VelocityIEPESensorSensitivityUnits)
            except Exception as e:  # noqa: BLE001
                # Some driver/module combinations reject excitation source NONE on
                # an IEPE-type channel; INTERNAL at 0 A is the equivalent, and is
                # what DAQExpress shows for the vibrometer channel.
                if spec.iepe:
                    raise
                self.notes.append(f"{spec.physical}: excitation NONE rejected ({e}); "
                                  f"using INTERNAL at 0 A instead")
                ch = self._add_channel(task, spec, ExcitationSource.INTERNAL, 0.0, fs,
                                       AccelUnits, VelocityUnits,
                                       VelocityIEPESensorSensitivityUnits)
            ch.ai_coupling = Coupling.AC if spec.coupling == "ac" else Coupling.DC

        buf = max(int(self.buffer_seconds * self.rate_hz), int(self.rate_hz))
        task.timing.cfg_samp_clk_timing(self.rate_hz,
                                        sample_mode=AcquisitionType.CONTINUOUS,
                                        samps_per_chan=buf)
        task.in_stream.input_buf_size = buf
        self.actual_rate_hz = float(task.timing.samp_clk_rate)
        self._reader = AnalogMultiChannelReader(task.in_stream)
        task.start()
        self._task = task

    @staticmethod
    def _add_channel(task, spec, src, val, fs, AccelUnits, VelocityUnits, VelSensUnits):
        if spec.kind == "velocity":
            return task.ai_channels.add_ai_velocity_iepe_chan(
                spec.physical, min_val=-fs, max_val=fs,
                units=VelocityUnits.METERS_PER_SECOND,
                sensitivity=spec.sensitivity,
                sensitivity_units=VelSensUnits.MILLIVOLTS_PER_MILLIMETER_PER_SECOND,
                current_excit_source=src, current_excit_val=val)
        return task.ai_channels.add_ai_accel_chan(
            spec.physical, min_val=-fs, max_val=fs, units=AccelUnits.G,
            sensitivity=spec.sensitivity,
            current_excit_source=src, current_excit_val=val)

    def read(self) -> np.ndarray | None:
        import nidaqmx
        if self._task is None:
            return None
        n = int(self._task.in_stream.avail_samp_per_chan)
        if n <= 0:
            return None
        # read_many_sample requires the array shape to match the request exactly
        # (not merely be large enough), and `avail` changes every poll — so the
        # buffer is resized whenever the count changes, not just when it grows.
        if self._buf.shape[1] != n:
            self._buf = np.empty((len(self.specs), n), dtype=np.float64)
        try:
            self._reader.read_many_sample(self._buf, number_of_samples_per_channel=n)
        except nidaqmx.errors.DaqReadError as e:      # type: ignore[attr-defined]
            if getattr(e, "error_code", None) == -200279:
                self.overflows += 1
                return None
            raise
        return self._buf

    def identity(self) -> str:
        try:
            import nidaqmx.system
            dev = self.specs[0].physical.split("/")[0]
            d = nidaqmx.system.Device(dev)
            sn = getattr(d, "serial_num", None) or d.dev_serial_num
            return f"{dev} {d.product_type} sn={sn}"
        except Exception:  # noqa: BLE001
            return self.specs[0].physical.split("/")[0]

    def close(self) -> None:
        if self._task is not None:
            try:
                self._task.stop()
            finally:
                self._task.close()
                self._task = None


# Sim scenarios: (broadband velocity floor uin/s RMS, [(freq Hz, velocity uin/s RMS)])
SIM_SCENARIOS = {
    "vc-e": (40.0, [(12.5, 60.0), (30.0, 35.0), (62.0, 25.0)]),
    "vc-c": (150.0, [(12.5, 260.0), (30.0, 140.0), (62.0, 90.0)]),
    "vc-a": (600.0, [(12.5, 1100.0), (30.0, 600.0), (62.0, 350.0)]),
    "office": (7000.0, [(12.5, 15000.0), (24.0, 9000.0), (62.0, 5400.0)]),
}


class SimStream:
    """Synthetic sensitive-space vibration — the same velocity on every channel.

    The laser channel gets that velocity directly; the accel channels get its
    derivative, so after the viewer integrates them all three traces should lie
    on top of each other. Footfall bursts every few seconds keep a demo alive.
    """

    def __init__(self, specs: list[ChannelSpec], rate_hz: float,
                 scenario: str = "vc-c", footfall_period_s: float = 6.0,
                 seed: int = 0) -> None:
        self.specs = specs
        self.rate_hz = rate_hz
        self.actual_rate_hz = rate_hz
        self.overflows = 0
        self.notes: list[str] = []
        self.floor_uin_s, self.tones = SIM_SCENARIOS[scenario]
        self.footfall_period_s = footfall_period_s
        self._rng = np.random.default_rng(seed)
        self._t0 = 0.0
        self._n = 0

    def start(self) -> None:
        self._t0 = time.monotonic()
        self._n = 0

    def identity(self) -> str:
        return "simulated NI-9234 (no hardware)"

    def read(self) -> np.ndarray | None:
        due = int((time.monotonic() - self._t0) * self.rate_hz)
        n = due - self._n
        if n <= 0:
            return None
        n = min(n, int(self.rate_hz))            # cap a stall-induced burst
        k = np.arange(self._n, self._n + n, dtype=np.float64)
        t = k / self.rate_hz
        self._n += n

        vel = np.zeros(n)                        # uin/s
        acc = np.zeros(n)                        # d(vel)/dt, uin/s^2
        for f, amp in self.tones:
            w = 2 * math.pi * f
            vel += amp * math.sqrt(2.0) * np.sin(w * t)
            acc += amp * math.sqrt(2.0) * w * np.cos(w * t)
        if self.footfall_period_s > 0:
            # Difference-of-exponentials envelope: starts at exactly zero (no
            # velocity step to differentiate) and has an analytic derivative, so
            # the accel channels stay a true derivative of the laser channel.
            phase = np.mod(t, self.footfall_period_s)
            mask = phase < 2.0
            decay, rise = 0.25, 0.02
            env = (np.exp(-phase / decay) - np.exp(-phase / rise)) * mask
            denv = (-np.exp(-phase / decay) / decay
                    + np.exp(-phase / rise) / rise) * mask
            w = 2 * math.pi * 5.0
            amp = 14.0 * self.floor_uin_s
            vel += amp * env * np.sin(w * t)
            acc += amp * (denv * np.sin(w * t) + env * w * np.cos(w * t))

        out = np.empty((len(self.specs), n), dtype=np.float64)
        for i, spec in enumerate(self.specs):
            noise = self._rng.normal(0.0, self.floor_uin_s, n)
            if spec.kind == "velocity":
                # laser: a bit more low-frequency noise, as in practice
                out[i] = (vel + 1.5 * noise) / UIN_PER_M
            else:
                # accel: white acceleration noise on top of the derivative
                dn = self._rng.normal(0.0, self.floor_uin_s * 2 * math.pi * 20.0, n)
                out[i] = (acc + dn) / UIN_PER_M / G0_M_S2
        return out

    def close(self) -> None:
        pass


# --------------------------------------------------------------------------- #
# Per-channel conversion to uin/s
# --------------------------------------------------------------------------- #
class ToVelocity:
    """Streaming converter: native DAQmx units -> velocity in uin/s.

    Filter state persists across blocks (``sosfilt`` with ``zi``), so the output
    is one continuous signal — no per-block transients or step discontinuities
    in the spectrum.
    """

    def __init__(self, kind: str, rate_hz: float, hp_hz: float = 1.0,
                 order: int = 4) -> None:
        self.kind = kind
        self.dt = 1.0 / rate_hz
        self.sos_pre = signal.butter(order, hp_hz, btype="highpass", fs=rate_hz,
                                     output="sos")
        self.sos_post = signal.butter(order, hp_hz, btype="highpass", fs=rate_hz,
                                      output="sos")
        self._zi_pre: np.ndarray | None = None
        self._zi_post: np.ndarray | None = None
        self._last_a = 0.0
        self._v_offset = 0.0
        self._leak = 0.999                       # bleeds integrator random-walk

    def _filt(self, sos, zi_attr: str, x: np.ndarray) -> np.ndarray:
        zi = getattr(self, zi_attr)
        if zi is None:
            zi = signal.sosfilt_zi(sos) * float(x[0])
            setattr(self, zi_attr, zi)
        y, zf = signal.sosfilt(sos, x, zi=zi)
        setattr(self, zi_attr, zf)
        return y

    def process(self, x: np.ndarray) -> np.ndarray:
        x = np.asarray(x, dtype=np.float64)
        if x.size == 0:
            return x
        if self.kind == "velocity":
            return self._filt(self.sos_pre, "_zi_pre", x) * UIN_PER_M
        a = x * G0_M_S2                                             # g -> m/s^2
        a = self._filt(self.sos_pre, "_zi_pre", a)
        prev = np.empty_like(a)
        prev[0] = self._last_a
        prev[1:] = a[:-1]
        v = self._v_offset + np.cumsum(0.5 * (a + prev)) * self.dt   # trapezoid
        self._last_a = float(a[-1])
        self._v_offset = float(v[-1]) * self._leak
        return self._filt(self.sos_post, "_zi_post", v) * UIN_PER_M


class Ring:
    """Fixed-length rolling buffer, (n_channels, n_samples), time-ordered."""

    def __init__(self, n_ch: int, n: int) -> None:
        self.buf = np.zeros((n_ch, n), dtype=np.float64)
        self.n = n
        self.pos = 0
        self.filled = 0

    def push(self, block: np.ndarray) -> None:
        m = block.shape[1]
        if m >= self.n:
            self.buf[:] = block[:, -self.n:]
            self.pos = 0
            self.filled = self.n
            return
        end = self.pos + m
        if end <= self.n:
            self.buf[:, self.pos:end] = block
        else:
            first = self.n - self.pos
            self.buf[:, self.pos:] = block[:, :first]
            self.buf[:, :m - first] = block[:, first:]
        self.pos = end % self.n
        self.filled = min(self.n, self.filled + m)

    def snapshot(self) -> np.ndarray:
        if self.filled < self.n:
            return self.buf[:, :self.filled].copy()
        return np.roll(self.buf, -self.pos, axis=1)


# --------------------------------------------------------------------------- #
# Spectrum helpers
# --------------------------------------------------------------------------- #
def minmax_decimate(t: np.ndarray, y: np.ndarray, n_out: int):
    """Thin a trace to ~``n_out`` points keeping each bucket's min and max.

    A 5 s window at 25.6 kS/s is 128k points per channel; drawing that many
    every frame is what makes a scope display stutter. Min/max pairs keep every
    visible spike while cutting the point count by one to two orders.
    """
    n = y.size
    if n <= n_out or n_out < 4:
        return t, y
    k = max(2, n // (n_out // 2))
    m = (n // k) * k
    buckets = y[:m].reshape(-1, k)
    out = np.empty(buckets.shape[0] * 2, dtype=y.dtype)
    out[0::2] = buckets.min(axis=1)
    out[1::2] = buckets.max(axis=1)
    tb = t[:m:k]
    to = np.empty_like(out)
    to[0::2] = tb
    to[1::2] = tb
    return to, out


def auto_nperseg(rate_hz: float, window_s: float, requested: int = 0) -> int:
    """Power-of-two segment giving ~2-5 Welch averages over the display window."""
    if requested:
        return int(requested)
    target = rate_hz * window_s / 2.5
    return int(2 ** max(8, round(math.log2(max(target, 256.0)))))


def welch_psd(x: np.ndarray, rate_hz: float, nperseg: int):
    nperseg = min(nperseg, x.shape[-1])
    f, pxx = signal.welch(x, fs=rate_hz, nperseg=nperseg, noverlap=nperseg // 2,
                          detrend="constant", scaling="density", axis=-1)
    return f, pxx


def third_octave_rms(f: np.ndarray, pxx: np.ndarray,
                     centers: np.ndarray = THIRD_OCTAVE_CENTERS) -> np.ndarray:
    """RMS per 1/3-octave band by integrating the PSD (signal units)."""
    df = float(f[1] - f[0])
    lo = centers / 2 ** (1 / 6)
    hi = centers * 2 ** (1 / 6)
    out = np.full(centers.shape, np.nan)
    for i in range(centers.size):
        m = (f >= lo[i]) & (f < hi[i])
        if m.any():
            out[i] = math.sqrt(float(np.sum(pxx[..., m])) * df)
    return out


# --------------------------------------------------------------------------- #
# Viewer
# --------------------------------------------------------------------------- #
def run_viewer(stream, specs: list[ChannelSpec], args) -> int:
    import pyqtgraph as pg
    from pyqtgraph.Qt import QtCore

    dark = not args.light
    pg.setConfigOption("background", "#101014" if dark else "w")
    pg.setConfigOption("foreground", "#d8d8e0" if dark else "k")
    # Antialiasing on a multi-thousand-point live trace is the classic frame-rate
    # killer; the decimated traces look clean without it.
    pg.setConfigOptions(antialias=False, useOpenGL=bool(args.opengl))

    rate = float(stream.actual_rate_hz)
    n_win = int(round(args.window * rate))
    ring = Ring(len(specs), n_win)
    procs = [ToVelocity(s.kind, rate, args.hp) for s in specs]
    nperseg = auto_nperseg(rate, args.window, args.nperseg)

    app = pg.mkQApp("Vibration - sensitive spaces")
    win = pg.GraphicsLayoutWidget(show=True, title="Vibration - sensitive spaces")
    win.resize(1680, 940)

    win.addLabel(
        f"<b>Velocity &mdash; micro-inches per second</b> &nbsp;|&nbsp; {stream.identity()}"
        f" &nbsp;|&nbsp; {rate:g} S/s &nbsp;|&nbsp; {args.window:g} s window"
        f" &nbsp;|&nbsp; high-pass {args.hp:g} Hz",
        row=0, col=0, colspan=2, size="12pt")

    trace_plots, trace_curves = [], []
    for i, spec in enumerate(specs):
        p = win.addPlot(row=i + 1, col=0)
        p.showGrid(x=True, y=True, alpha=0.25)
        p.setMouseEnabled(x=False, y=False)
        p.setLabel("left", "uin/s")
        p.setXRange(-args.window, 0.0, padding=0)
        if i < len(specs) - 1:
            p.getAxis("bottom").setStyle(showValues=False)
        else:
            p.setLabel("bottom", "seconds (now = 0)")
        c = p.plot(pen=pg.mkPen(TRACE_COLORS[i % len(TRACE_COLORS)], width=1.4))
        c.setClipToView(True)
        trace_plots.append(p)
        trace_curves.append(c)

    spec_plot = win.addPlot(row=1, col=1, rowspan=len(specs))
    spec_plot.showGrid(x=True, y=True, alpha=0.25)
    spec_plot.setLogMode(x=True, y=True)
    spec_plot.setLabel("bottom", "frequency (Hz)")
    spec_plot.setXRange(math.log10(args.fmin), math.log10(args.fmax * 1.7), padding=0)
    spec_plot.setMouseEnabled(x=False, y=False)
    spec_plot.addLegend(offset=(-10, 10))
    spec_curves, hold_curves = [], []
    for i, spec in enumerate(specs):
        col = TRACE_COLORS[i % len(TRACE_COLORS)]
        spec_curves.append(spec_plot.plot(pen=pg.mkPen(col, width=1.6),
                                          name=spec.label or spec.physical))
        hold_curves.append(spec_plot.plot(
            pen=pg.mkPen(col, width=1.0, style=QtCore.Qt.PenStyle.DashLine)))

    # VC reference lines (flat 8-80 Hz part of each criterion).
    vc_items = []
    for label, limit in VC_CRITERIA:
        line = spec_plot.plot([VC_BAND_LO_HZ, VC_BAND_HI_HZ], [limit, limit],
                              pen=pg.mkPen("#8a8aa0", width=1.0,
                                           style=QtCore.Qt.PenStyle.DotLine))
        txt = pg.TextItem(label, color="#9a9ab0", anchor=(0, 0.5))
        txt.setPos(math.log10(VC_BAND_HI_HZ * 1.05), math.log10(limit))
        spec_plot.addItem(txt)
        vc_items += [line, txt]

    status = win.addLabel("", row=len(specs) + 1, col=0, colspan=2, size="11pt")

    state = {
        "paused": False, "mode": "third", "hold": bool(args.peak_hold),
        "common": not args.per_channel_scale, "ylim": [1.0] * len(specs),
        "hold_data": [None] * len(specs), "spec_hi": 1e4,
        "frames": 0, "t_start": time.monotonic(),
    }

    def reset_hold():
        state["hold_data"] = [None] * len(specs)
        for c in hold_curves:
            c.setData([], [])

    def set_spectrum_mode(mode: str):
        state["mode"] = mode
        reset_hold()
        if mode == "psd":
            spec_plot.setLabel("left", "velocity spectral density (uin/s/sqrt(Hz))")
        else:
            spec_plot.setLabel("left", "1/3-octave RMS velocity (uin/s)")
        for it in vc_items:
            it.setVisible(mode == "third")

    set_spectrum_mode("psd" if args.start_view == "psd" else "third")

    def on_key(ev):
        k = ev.key()
        K = QtCore.Qt.Key
        if k in (K.Key_Q, K.Key_Escape):
            win.close()
        elif k == K.Key_Space:
            state["paused"] = not state["paused"]
        elif k == K.Key_V:
            set_spectrum_mode("psd" if state["mode"] == "third" else "third")
        elif k == K.Key_P:
            state["hold"] = not state["hold"]
            reset_hold()
        elif k == K.Key_C:
            state["common"] = not state["common"]
        elif k == K.Key_A:
            state["ylim"] = [1.0] * len(specs)
            state["spec_hi"] = 1e2
        elif k == K.Key_S:
            path = time.strftime("vibration_%Y%m%d_%H%M%S.png")
            win.grab().save(path)
            print(f"screenshot: {path}")
        else:
            pg.GraphicsLayoutWidget.keyPressEvent(win, ev)

    win.keyPressEvent = on_key

    def update():
        try:
            block = stream.read()
        except Exception as e:  # noqa: BLE001 — report once, don't spam a demo
            timer.stop()
            print(f"acquisition stopped: {type(e).__name__}: {e}", file=sys.stderr)
            status.setText(f"<span style='color:#ff5c8a'>acquisition stopped: "
                           f"{type(e).__name__}: {e}</span>")
            return
        if block is not None and block.shape[1] > 0:
            conv = np.empty_like(block)
            for i, proc in enumerate(procs):
                conv[i] = proc.process(block[i])
            ring.push(conv)
        if state["paused"] or ring.filled < int(0.5 * rate):
            return
        state["frames"] += 1

        data = ring.snapshot()
        n = data.shape[1]
        t = np.arange(-n + 1, 1) / rate

        rms = np.sqrt(np.mean(data ** 2, axis=1))
        pk = np.max(np.abs(data), axis=1)
        lim_common = float(np.max(pk)) * 1.25
        # Numbers settle at ~5 Hz: easier to read off a projector, and text
        # relayout is expensive enough to matter at 20 fps.
        text_frame = state["frames"] % max(1, int(round(args.fps / 5))) == 0
        for i, spec in enumerate(specs):
            td, yd = minmax_decimate(t, data[i], args.trace_points)
            trace_curves[i].setData(td, yd)
            target = max(lim_common if state["common"] else float(pk[i]) * 1.25, 1.0)
            prev = state["ylim"][i]
            new = target if target > prev else 0.92 * prev + 0.08 * target
            if abs(new - prev) > 0.02 * prev:
                state["ylim"][i] = new
                trace_plots[i].setYRange(-new, new, padding=0)
            if text_frame:
                trace_plots[i].setTitle(
                    f"<span style='color:{TRACE_COLORS[i % len(TRACE_COLORS)]}'>"
                    f"{spec.label or spec.physical}</span> &nbsp; "
                    f"RMS <b>{rms[i]:,.0f}</b> &nbsp; peak <b>{pk[i]:,.0f}</b> uin/s")

        f, pxx = welch_psd(data, rate, nperseg)
        classes, ymax = [], 1e-2
        for i in range(len(specs)):
            bands = third_octave_rms(f, pxx[i])
            cls, peak = vc_class(THIRD_OCTAVE_CENTERS, bands)
            classes.append(f"{specs[i].label or specs[i].physical}: <b>{cls}</b> "
                           f"({peak:,.0f} uin/s)")
            if state["mode"] == "psd":
                m = (f >= args.fmin) & (f <= args.fmax) & (pxx[i] > 0)
                x, y = f[m], np.sqrt(pxx[i][m])
            else:
                m = ((THIRD_OCTAVE_CENTERS >= args.fmin)
                     & (THIRD_OCTAVE_CENTERS <= args.fmax) & np.isfinite(bands))
                x, y = THIRD_OCTAVE_CENTERS[m], bands[m]
            spec_curves[i].setData(x, y)
            if y.size:
                ymax = max(ymax, float(np.max(y)))
            if state["hold"] and y.size:
                h = state["hold_data"][i]
                state["hold_data"][i] = (y if h is None or h.shape != y.shape
                                         else np.maximum(h, y))
                hold_curves[i].setData(x, state["hold_data"][i])
                ymax = max(ymax, float(np.max(state["hold_data"][i])))
            elif not state["hold"]:
                hold_curves[i].setData([], [])

        hi = max(ymax * 3.0, 0.9 * state["spec_hi"])
        if abs(hi - state["spec_hi"]) > 0.02 * state["spec_hi"]:
            state["spec_hi"] = hi
            spec_plot.setYRange(math.log10(max(hi / 1e5, 1e-3)), math.log10(hi),
                                padding=0)

        if not text_frame:
            return
        fps = state["frames"] / max(1e-3, time.monotonic() - state["t_start"])
        extra = f" &nbsp;|&nbsp; overflows: {stream.overflows}" if stream.overflows else ""
        mode = "1/3-octave + VC" if state["mode"] == "third" else "PSD"
        status.setText(
            " &nbsp;|&nbsp; ".join(classes)
            + f" &nbsp;|&nbsp; {mode}{' (peak hold)' if state['hold'] else ''}"
            + f" &nbsp;|&nbsp; {fps:.0f} fps{extra}"
            + " &nbsp;|&nbsp; space pause &middot; v view &middot; p hold &middot; "
              "c scale &middot; s shot &middot; q quit")

    timer = QtCore.QTimer()
    timer.timeout.connect(update)
    timer.start(int(1000 / args.fps))

    if args.run_seconds > 0:                      # unattended smoke test / kiosk
        QtCore.QTimer.singleShot(int(args.run_seconds * 1000), app.quit)

    for note in stream.notes:
        print(f"note: {note}")
    code = app.exec()
    if args.run_seconds > 0:
        print(f"ran {state['frames']} frames in {args.run_seconds:g} s "
              f"({state['frames'] / args.run_seconds:.1f} fps), "
              f"overflows: {stream.overflows}")
    stream.close()
    return int(code)


# --------------------------------------------------------------------------- #
# Self-test (no hardware, no GUI)
# --------------------------------------------------------------------------- #
def selftest(args) -> int:
    """Drive the DSP chain with a known signal and check the numbers.

    A 20 Hz velocity tone of 1000 uin/s RMS is fed in two ways — as velocity
    (laser path) and as its derivative (accel path) — and must come back out at
    1000 uin/s RMS in the trace, in the 20 Hz 1/3-octave band, and classify VC-B.
    """
    rate = args.rate
    f_tone, v_rms = 20.0, 1000.0
    secs = 12.0
    n = int(rate * secs)
    t = np.arange(n) / rate
    w = 2 * math.pi * f_tone
    vel_uin_s = v_rms * math.sqrt(2.0) * np.sin(w * t)
    acc_uin_s2 = v_rms * math.sqrt(2.0) * w * np.cos(w * t)

    cases = [
        ("laser (velocity, m/s)", "velocity", vel_uin_s / UIN_PER_M),
        ("accel (g, integrated)", "accel", acc_uin_s2 / UIN_PER_M / G0_M_S2),
    ]
    ok = True
    print(f"self-test: {f_tone:g} Hz tone, {v_rms:,.0f} uin/s RMS, "
          f"{rate:g} S/s, HP {args.hp:g} Hz\n")
    print(f"{'path':24s} {'trace RMS':>12s} {'band RMS':>12s} {'VC':>6s}  result")
    nperseg = auto_nperseg(rate, args.window, args.nperseg)
    for name, kind, x in cases:
        proc = ToVelocity(kind, rate, args.hp)
        y = np.concatenate([proc.process(chunk)
                            for chunk in np.array_split(x, int(secs * 10))])
        y = y[int(2 * rate):]                       # drop filter start-up
        trace_rms = float(np.sqrt(np.mean(y ** 2)))
        f, pxx = welch_psd(y, rate, nperseg)
        bands = third_octave_rms(f, pxx)
        band_rms = float(bands[THIRD_OCTAVE_CENTERS == f_tone][0])
        cls, _ = vc_class(THIRD_OCTAVE_CENTERS, bands)
        good = (abs(trace_rms / v_rms - 1) < 0.02
                and abs(band_rms / v_rms - 1) < 0.05 and cls == "VC-B")
        ok &= good
        print(f"{name:24s} {trace_rms:12,.1f} {band_rms:12,.1f} {cls:>6s}  "
              f"{'PASS' if good else 'FAIL'}")

    # Criterion table: a level just under each limit must classify as that curve.
    for label, limit in VC_CRITERIA:
        bands = np.full(THIRD_OCTAVE_CENTERS.shape, np.nan)
        bands[THIRD_OCTAVE_CENTERS == 31.5] = limit * 0.99
        cls, _ = vc_class(THIRD_OCTAVE_CENTERS, bands)
        if cls != label:
            print(f"FAIL: {limit:,.0f} uin/s classified {cls!r}, expected {label!r}")
            ok = False
    print("\nself-test:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_specs(args) -> list[ChannelSpec]:
    specs: list[ChannelSpec] = []
    for ai in [c.strip() for c in args.vel_chans.split(",") if c.strip()]:
        specs.append(ChannelSpec(physical=f"{args.device}/{ai}", kind="velocity",
                                 sensitivity=args.vel_sens, iepe=args.vel_iepe,
                                 excitation_a=args.excitation, coupling=args.coupling,
                                 label=f"{ai} laser vibrometer"))
    for ai in [c.strip() for c in args.accel_chans.split(",") if c.strip()]:
        specs.append(ChannelSpec(physical=f"{args.device}/{ai}", kind="accel",
                                 sensitivity=args.accel_sens, iepe=True,
                                 excitation_a=args.excitation, coupling=args.coupling,
                                 label=f"{ai} accelerometer"))
    labels = [s.strip() for s in args.labels.split(",")] if args.labels else []
    for i, lab in enumerate(labels[:len(specs)]):
        if lab:
            specs[i].label = lab
    return specs


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--device", default="cDAQ1Mod1", help="NI-9234 name from NI MAX")
    p.add_argument("--vel-chans", default="ai0", help="velocimeter channels (comma list)")
    p.add_argument("--vel-sens", type=float, default=12.5, help="mV/(mm/s) [12.5]")
    p.add_argument("--vel-iepe", action="store_true",
                   help="feed IEPE current to the vibrometer (default OFF - most "
                        "vibrometer voltage outputs must not be current-driven)")
    p.add_argument("--accel-chans", default="ai1,ai2", help="accelerometer channels")
    p.add_argument("--accel-sens", type=float, default=1000.0, help="mV/g [1000]")
    p.add_argument("--excitation", type=float, default=0.002, help="IEPE current, A [0.002]")
    p.add_argument("--coupling", choices=["ac", "dc"], default="ac")
    p.add_argument("--labels", default="", help="comma-separated display names")

    p.add_argument("--rate", type=float, default=2560.0,
                   help="sample rate, must be native 51200/n [2560]")
    p.add_argument("--window", type=float, default=5.0, help="time-view seconds [5]")
    p.add_argument("--hp", type=float, default=1.0, help="high-pass corner, Hz [1]")
    p.add_argument("--fmin", type=float, default=1.0, help="spectrum min Hz [1]")
    p.add_argument("--fmax", type=float, default=100.0, help="spectrum max Hz [100]")
    p.add_argument("--nperseg", type=int, default=0, help="Welch segment (0 = auto)")
    p.add_argument("--fps", type=float, default=20.0, help="display updates/s [20]")
    p.add_argument("--trace-points", type=int, default=2000,
                   help="points drawn per trace after min/max decimation [2000]")
    p.add_argument("--opengl", action="store_true",
                   help="GPU-accelerated drawing (faster, but driver-dependent)")
    p.add_argument("--start-view", choices=["vc", "psd"], default="vc",
                   help="spectrum panel at startup: 1/3-octave with VC lines, or PSD [vc]")
    p.add_argument("--peak-hold", action="store_true", help="start with peak hold on")
    p.add_argument("--per-channel-scale", action="store_true",
                   help="scale each trace independently (default: common scale)")
    p.add_argument("--light", action="store_true", help="light theme")

    p.add_argument("--sim", action="store_true", help="synthetic signals, no hardware")
    p.add_argument("--scenario", choices=sorted(SIM_SCENARIOS), default="vc-c",
                   help="--sim vibration level [vc-c]")
    p.add_argument("--selftest", action="store_true",
                   help="validate the DSP chain and exit (no hardware, no GUI)")
    p.add_argument("--run-seconds", type=float, default=0.0,
                   help="close automatically after N seconds (smoke test / kiosk)")
    args = p.parse_args(argv)

    if not is_native_rate(args.rate):
        near = nearest_native_rate(args.rate)
        print(f"error: {args.rate:g} S/s is not a native NI-9234 rate (51200/n, n=1..31).\n"
              f"       The driver would coerce it to {near:g} S/s - ask for that instead.",
              file=sys.stderr)
        return 2

    if args.selftest:
        return selftest(args)

    specs = build_specs(args)
    if not specs:
        print("error: no channels configured", file=sys.stderr)
        return 2
    print("Channels:")
    for s in specs:
        print(f"  {s.describe()}")
    print()

    stream = (SimStream(specs, args.rate, scenario=args.scenario) if args.sim
              else NidaqmxStream(specs, args.rate))
    stream.start()
    if abs(stream.actual_rate_hz - args.rate) > 1e-6:
        print(f"note: driver is running at {stream.actual_rate_hz:g} S/s "
              f"(requested {args.rate:g})")
    return run_viewer(stream, specs, args)


if __name__ == "__main__":
    raise SystemExit(main())
