"""Energy meter of one connector: Energy.Active.Import.Register, from power x elapsed time.

Pure, no I/O: time comes from the injected clock. It lives on the VirtualCharger, not on a
connection, so like a real meter it keeps counting across reconnects.
"""

from datetime import timedelta

from ocpp_ops.clock import Clock

# 1 Wh = 3600 J = 3.6e9 µJ.
MICROJOULES_PER_WH = 3_600_000_000
_ONE_MICROSECOND = timedelta(microseconds=1)


class EnergyMeter:
    """Integrates power over time. Energy is kept in µJ, an integer.

    Watts x microseconds is exactly µJ, so the total never drifts the way a float sum of
    fractional Wh would, and tests can assert exact values. The register is reported in
    whole Wh, rounded down: OCPP 1.6 meterStart/meterStop are integers in Wh (§6.45), and
    rounding down keeps it monotonic, as §7.31 requires.
    """

    def __init__(self, now: Clock) -> None:
        self._now = now
        self._energy_uj = 0
        self._power_w = 0
        self._since = now()  # energy is integrated up to this time

    @property
    def power_w(self) -> int:
        """Power drawn right now (Power.Active.Import), in W."""
        return self._power_w

    def set_power(self, watts: int) -> None:
        """Change the power from now on. Energy up to now is counted at the old power."""
        if watts < 0:
            raise ValueError("power must be >= 0: the meter only counts imported energy")
        self._integrate()
        self._power_w = watts

    def register_wh(self) -> int:
        """Energy.Active.Import.Register now, in whole Wh."""
        self._integrate()
        return self._energy_uj // MICROJOULES_PER_WH

    def _integrate(self) -> None:
        now = self._now()
        elapsed_us = (now - self._since) // _ONE_MICROSECOND  # exact integer division
        self._energy_uj += self._power_w * elapsed_us
        self._since = now
