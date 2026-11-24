"""Smoke tests: the package and the OCPP 1.6 library import correctly."""

import ocpp_ops


def test_package_imports() -> None:
    assert ocpp_ops.__version__ == "0.1.0"


def test_ocpp_v16_imports() -> None:
    from ocpp.v16 import ChargePoint, call, call_result

    assert ChargePoint is not None
    assert hasattr(call, "BootNotification")
    assert hasattr(call_result, "BootNotification")
