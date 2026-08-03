"""Reader backends for :class:`~nidaq_viblog.nidaq_source.NidaqSource`.

The source logic (sample counter → tick, module splitting, overflow → gap) is
hardware-independent, so it talks to a small :class:`ReaderBackend` interface
instead of ``nidaqmx`` directly. Two implementations:

* :class:`NidaqmxBackend` — the real DAQmx task (lazy-imports ``nidaqmx`` so the
  package, tests, and simulate mode never need the NI driver installed, exactly
  as viblog keeps ``mscl`` inside ``mscl_io``); and
* :class:`SimBackend` — a numpy signal generator with truthful buffer semantics
  (backlog, capacity-bounded overflow that advances the acquired counter) so the
  full drain/ledger path is exercised in unit tests without a chassis.

Both present the same contract the source relies on::

    first_index_of_this_read = total_acquired() - avail()

which is what makes an overflow surface as a jump in the sample counter (hence a
ledger gap) with no special-casing in the source.
"""

from __future__ import annotations

import logging
import time
from typing import Callable, Protocol

import numpy as np

log = logging.getLogger(__name__)

# DAQmx "attempted to read samples no longer available" — the wired analog of a
# permanent radio loss. Exact code is (verify) on this stack (spike 04).
DAQMX_OVERWRITE_ERR = -200279


class Overflow(Exception):
    """Raised by a backend's read when the host buffer overwrote unread samples.

    Carries no count: the number lost is inferred by the source from the jump in
    ``total_acquired() - avail()`` on the next poll (→ ledger gap).
    """


class ChannelPlan:
    """Flat, task-order description of the channels a backend will acquire."""

    def __init__(self, physical: list[str], sensitivities_mv_per_g: list[float],
                 iepe: list[bool], excitation_a: list[float], coupling: list[str]) -> None:
        self.physical = physical
        self.sensitivities_mv_per_g = sensitivities_mv_per_g
        self.iepe = iepe
        self.excitation_a = excitation_a
        self.coupling = coupling

    @property
    def n(self) -> int:
        return len(self.physical)


class ReaderBackend(Protocol):
    def bias_read(self) -> list[float]:
        """DC-coupled bias voltage per channel (task order); [] if not performed."""
        ...

    def start(self) -> dict:
        """Start continuous acquisition. Returns provenance:
        ``{"actual_rate_hz": float, "identity": {device: {...}}}``."""
        ...

    def avail(self) -> int:
        """Samples-per-channel currently available to read (0 if none)."""
        ...

    def total_acquired(self) -> int:
        """Cumulative samples-per-channel the device has acquired since start."""
        ...

    def read_into(self, out: np.ndarray, n: int) -> None:
        """Read ``n`` samples/channel into ``out`` (shape ``[plan.n, >=n]``).

        Raises :class:`Overflow` if the buffer overwrote unread data.
        """
        ...

    def close(self) -> None:
        ...


# --------------------------------------------------------------------------- #
# Real hardware
# --------------------------------------------------------------------------- #
class NidaqmxBackend:
    """DAQmx continuous accel task over one or more NI-9234 modules.

    All channels go in a single task so they share the sample clock →
    simultaneous sampling across modules (ARCHITECTURE.md §3). Only this class
    imports ``nidaqmx``.
    """

    def __init__(self, plan: ChannelPlan, rate_hz: float, buffer_seconds: float = 8.0,
                 bias_seconds: float = 0.2) -> None:
        self.plan = plan
        self.rate_hz = rate_hz
        self.buffer_seconds = buffer_seconds
        self.bias_seconds = bias_seconds
        self._task = None
        self._reader = None
        self._actual_rate = rate_hz

    # -- lifecycle ---------------------------------------------------------- #
    def bias_read(self) -> list[float]:
        """Short DC-coupled read per IEPE channel → steady bias voltage.

        Healthy IEPE sensors sit at their compliance bias (~8–12 V); ~0 V means a
        short, near-rail means open/unpowered (classified in nidaq_source). Done
        in a throwaway voltage task so the real accel task starts clean/AC-coupled.
        """
        import nidaqmx
        from nidaqmx.constants import (Coupling, ExcitationSource,
                                       TerminalConfiguration)

        biases: list[float] = []
        with nidaqmx.Task() as t:
            for i, phys in enumerate(self.plan.physical):
                if not self.plan.iepe[i]:
                    biases.append(float("nan"))
                    continue
                ch = t.ai_channels.add_ai_voltage_chan(
                    phys, terminal_config=TerminalConfiguration.PSEUDO_DIFF,
                    min_val=-30.0, max_val=30.0)
                ch.ai_coupling = Coupling.DC
                ch.ai_excit_src = ExcitationSource.INTERNAL
                ch.ai_excit_val = self.plan.excitation_a[i]
            iepe_idx = [i for i in range(self.plan.n) if self.plan.iepe[i]]
            if not iepe_idx:
                return [float("nan")] * self.plan.n
            n = max(1, int(self.bias_seconds * self.rate_hz))
            t.timing.cfg_samp_clk_timing(self.rate_hz, samps_per_chan=n)
            data = np.asarray(t.read(number_of_samples_per_channel=n))
            if data.ndim == 1:
                data = data[np.newaxis, :]
            means = data.mean(axis=1)
            j = 0
            for i in range(self.plan.n):
                if self.plan.iepe[i]:
                    biases.append(float(means[j])); j += 1
                else:
                    biases.append(float("nan"))
        return biases

    def start(self) -> dict:
        import nidaqmx
        from nidaqmx.constants import (AccelUnits, AcquisitionType, Coupling,
                                       ExcitationSource)
        from nidaqmx.stream_readers import AnalogMultiChannelReader

        task = nidaqmx.Task()
        for i, phys in enumerate(self.plan.physical):
            excit = (ExcitationSource.INTERNAL if self.plan.iepe[i]
                     else ExcitationSource.NONE)
            ch = task.ai_channels.add_ai_accel_chan(
                phys, sensitivity=self.plan.sensitivities_mv_per_g[i],
                units=AccelUnits.G, current_excit_source=excit,
                current_excit_val=self.plan.excitation_a[i])
            ch.ai_coupling = (Coupling.AC if self.plan.coupling[i] == "ac"
                              else Coupling.DC)
        buf = max(int(self.buffer_seconds * self.rate_hz), int(self.rate_hz))
        task.timing.cfg_samp_clk_timing(
            self.rate_hz, sample_mode=AcquisitionType.CONTINUOUS,
            samps_per_chan=buf)
        task.in_stream.input_buf_size = buf
        self._actual_rate = float(task.timing.samp_clk_rate)  # driver-coerced truth
        self._reader = AnalogMultiChannelReader(task.in_stream)
        identity = self._identity(task)
        task.start()
        self._task = task
        return {"actual_rate_hz": self._actual_rate, "identity": identity}

    def _identity(self, task) -> dict:
        out: dict = {}
        seen: set[str] = set()
        for chan in task.ai_channels:
            dev = chan.physical_channel.name.split("/")[0]
            if dev in seen:
                continue
            seen.add(dev)
            try:
                import nidaqmx.system
                d = nidaqmx.system.Device(dev)
                out[dev] = {"product_type": d.product_type,
                            "serial_num": d.dev_serial_num}
            except Exception as e:  # noqa: BLE001 — provenance is best-effort
                out[dev] = {"error": f"{type(e).__name__}: {e}"}
        return out

    # -- draining ----------------------------------------------------------- #
    def avail(self) -> int:
        return int(self._task.in_stream.avail_samp_per_chan) if self._task else 0

    def total_acquired(self) -> int:
        return int(self._task.in_stream.total_samp_per_chan_acquired) if self._task else 0

    def read_into(self, out: np.ndarray, n: int) -> None:
        import nidaqmx
        try:
            self._reader.read_many_sample(out, number_of_samples_per_channel=n)
        except nidaqmx.errors.DaqReadError as e:  # type: ignore[attr-defined]
            if getattr(e, "error_code", None) == DAQMX_OVERWRITE_ERR:
                raise Overflow(str(e)) from e
            raise

    def close(self) -> None:
        if self._task is not None:
            try:
                self._task.stop()
            finally:
                self._task.close()
                self._task = None


# --------------------------------------------------------------------------- #
# Simulation (no hardware, no nidaqmx)
# --------------------------------------------------------------------------- #
class SimBackend:
    """Signal generator with the same buffer contract as the real backend.

    Paced by an injectable clock. Produces ``fs * elapsed`` samples/channel; the
    host buffer holds at most ``capacity`` unread samples, so a consumer that
    falls behind loses the oldest ones — the acquired counter keeps climbing,
    which is exactly the overflow the source must turn into a ledger gap.
    """

    def __init__(self, plan: ChannelPlan, rate_hz: float,
                 tones: list[tuple[float, float]] | None = None,
                 noise_g: float = 2e-4, capacity_samples: int | None = None,
                 bias_v: list[float] | None = None,
                 time_fn: Callable[[], float] = time.monotonic, seed: int = 0) -> None:
        self.plan = plan
        self.rate_hz = rate_hz
        self.tones = tones if tones is not None else [(12.5, 1e-3), (50.0, 5e-4)]
        self.noise_g = noise_g
        self.capacity = capacity_samples or max(int(8 * rate_hz), int(rate_hz))
        self.bias_v = bias_v if bias_v is not None else [9.5] * plan.n
        self.time_fn = time_fn
        self._rng = np.random.default_rng(seed)
        self._t0: float | None = None
        self._consumed = 0           # index of the next sample the consumer gets
        self._last_produced = 0
        self._pending_overflow = False

    def bias_read(self) -> list[float]:
        return [self.bias_v[i] if self.plan.iepe[i] else float("nan")
                for i in range(self.plan.n)]

    def start(self) -> dict:
        self._t0 = self.time_fn()
        self._consumed = 0
        self._last_produced = 0
        return {"actual_rate_hz": self.rate_hz,
                "identity": {f"sim{i}": {"product_type": "NI-9234 (sim)",
                                         "serial_num": f"SIM{i:04d}"}
                             for i in range(1)}}

    def _produced(self) -> int:
        assert self._t0 is not None, "start() not called"
        return int((self.time_fn() - self._t0) * self.rate_hz)

    def avail(self) -> int:
        produced = self._produced()
        backlog = produced - self._consumed
        if backlog > self.capacity:
            lost = backlog - self.capacity
            self._consumed += lost          # oldest unread samples overwritten
            self._pending_overflow = True
            backlog = self.capacity
        self._last_produced = produced
        return max(0, backlog)

    def total_acquired(self) -> int:
        return self._last_produced

    def read_into(self, out: np.ndarray, n: int) -> None:
        if self._pending_overflow:
            self._pending_overflow = False
            raise Overflow(f"sim buffer overwrite (consumed jumped to {self._consumed})")
        idx = np.arange(self._consumed, self._consumed + n)
        t = idx / self.rate_hz
        for ch in range(self.plan.n):
            sig = np.zeros(n, dtype=np.float64)
            for f, amp in self.tones:
                sig += amp * np.sin(2 * np.pi * f * t + ch)
            sig += self._rng.normal(0.0, self.noise_g, n)
            out[ch, :n] = sig
        self._consumed += n

    def close(self) -> None:
        pass
