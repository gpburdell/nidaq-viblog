"""NI-9234 rate ladder."""

from nidaq_viblog import rates


def test_landmark_rates_are_native():
    for r in (51200, 25600, 12800, 2560, 51200 / 3):
        assert rates.is_native(r), r


def test_off_ladder_rates_rejected():
    # 512 (n=100) and 1024 (n=50) need divisors past n=31, so are unreachable.
    for r in (512, 1024, 2000, 5000):
        assert not rates.is_native(r), r


def test_2048_is_native_correcting_the_doc():
    # HARDWARE_NOTES said "no native 512/1024/2048" but 2048 = 51200/25 (n=25<=31)
    # is exactly on the ladder. Pure arithmetic, not hardware-dependent.
    assert rates.is_native(2048)
    assert rates.divisor_for(2048) == 25


def test_divisor_for_known_points():
    assert rates.divisor_for(51200) == 1
    assert rates.divisor_for(2560) == 20   # the NIH-dataset rate
    assert rates.divisor_for(25600) == 2
    assert rates.divisor_for(2000) is None


def test_nearest_native_is_on_ladder_and_close():
    for req in (500, 2000, 3000, 40000, 60000):
        got = rates.nearest_native_rate(req)
        assert rates.is_native(got)
        # the coerced rate is the closest achievable divisor
        assert abs(got - req) <= abs(rates.NI9234_BASE_HZ - req)


def test_native_rates_span_documented_range():
    rs = rates.native_rates()
    assert rs[0] == 51200.0
    assert abs(rs[-1] - 51200 / 31) < 1e-6   # ~1651.6 S/s minimum
