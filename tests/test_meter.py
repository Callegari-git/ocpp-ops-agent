import pytest

from fakes import FakeClock
from ocpp_ops.charger.meter import EnergyMeter


def test_energy_is_power_times_time() -> None:
    clock = FakeClock()
    meter = EnergyMeter(clock)
    meter.set_power(11_000)

    clock.advance(3600)

    assert meter.register_wh() == 11_000  # 11 kW for 1 h


def test_partial_wh_is_rounded_down_and_kept() -> None:
    clock = FakeClock()
    meter = EnergyMeter(clock)
    meter.set_power(7_400)

    clock.advance(60)  # 7400 W x 60 s = 123.33 Wh
    assert meter.register_wh() == 123
    clock.advance(120)  # 370 Wh in total: the fractions were not lost
    assert meter.register_wh() == 370


def test_power_change_counts_each_part_at_its_power() -> None:
    clock = FakeClock()
    meter = EnergyMeter(clock)
    meter.set_power(3_600)
    clock.advance(1800)  # 1800 Wh
    meter.set_power(0)
    clock.advance(1000)  # nothing
    meter.set_power(7_200)
    clock.advance(0.5)  # 1 Wh

    assert meter.register_wh() == 1801
    assert meter.power_w == 7_200


def test_no_power_no_energy() -> None:
    clock = FakeClock()
    meter = EnergyMeter(clock)
    clock.advance(10_000)

    assert (meter.register_wh(), meter.power_w) == (0, 0)


def test_negative_power_is_refused() -> None:
    with pytest.raises(ValueError, match="imported energy"):
        EnergyMeter(FakeClock()).set_power(-1)
