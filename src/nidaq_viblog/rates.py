"""NI-9234 native sample-rate ladder.

The NI-9234's delta-sigma converters derive every data rate from the module's
master timebase (HARDWARE_NOTES.md):

    fs = 13.1072 MHz / 256 / n = 51200 / n,   n = 1 .. 31

so the only achievable rates are 51200/n. A requested rate that is not on this
ladder is *coerced* by the driver to the nearest achievable rate; the real task
must therefore always read back ``task.timing.samp_clk_rate`` (spike 02).

These numbers are the documented ladder; ``(verify)`` items in HARDWARE_NOTES
(notably the exact ``n`` range) are confirmed on the bench in phase 1. Keeping
the ladder here — rather than trusting a requested rate — is what lets config
validation warn *before* a run that a rate will be silently coerced.
"""

from __future__ import annotations

# 13.1072 MHz / 256. The base numerator of the rate ladder.
NI9234_BASE_HZ = 13_107_200 / 256  # = 51200.0
# Divisor range n = 1..31 (upper bound flagged (verify) in HARDWARE_NOTES).
NI9234_N_MIN = 1
NI9234_N_MAX = 31


def native_rates() -> list[float]:
    """All achievable NI-9234 rates, highest first (51200 .. ~1651.6 S/s)."""
    return [NI9234_BASE_HZ / n for n in range(NI9234_N_MIN, NI9234_N_MAX + 1)]


def divisor_for(rate_hz: float) -> int | None:
    """The integer divisor ``n`` giving exactly ``rate_hz``, or None if off-ladder."""
    if rate_hz <= 0:
        return None
    n = NI9234_BASE_HZ / rate_hz
    n_int = round(n)
    if NI9234_N_MIN <= n_int <= NI9234_N_MAX and abs(n - n_int) < 1e-6:
        return n_int
    return None


def is_native(rate_hz: float) -> bool:
    """True if ``rate_hz`` is exactly on the NI-9234 ladder."""
    return divisor_for(rate_hz) is not None


def nearest_native_rate(rate_hz: float) -> float:
    """The ladder rate the driver would coerce ``rate_hz`` to (nearest divisor).

    Mirrors the driver's coercion so config validation can show the operator the
    rate they will *actually* get. Ties break toward the higher rate (smaller n),
    which is how NI-DAQmx rounds the timebase divisor in practice (verify: spike 02).
    """
    if rate_hz <= 0:
        raise ValueError(f"rate must be positive, got {rate_hz}")
    n = NI9234_BASE_HZ / rate_hz
    n_clamped = min(max(n, NI9234_N_MIN), NI9234_N_MAX)
    # nearest integer divisor, ties -> smaller n (higher rate)
    lo, hi = int(n_clamped), int(n_clamped) + 1
    lo = max(lo, NI9234_N_MIN)
    hi = min(hi, NI9234_N_MAX)
    cand = sorted({lo, hi}, key=lambda k: (abs(NI9234_BASE_HZ / k - rate_hz), k))
    return NI9234_BASE_HZ / cand[0]
