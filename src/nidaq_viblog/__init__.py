"""nidaq-viblog — wired vibration logging on NI DAQ hardware.

A thin NI-specific acquisition source (:class:`nidaq_viblog.nidaq_source.NidaqSource`)
plugged into the viblog stack (sinks, DSP, trigger, live UI, review) via viblog's
``SweepSource`` protocol. See docs/ARCHITECTURE.md.
"""

from __future__ import annotations

__version__ = "0.1.0"
