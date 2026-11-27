"""Outbound commands (CSMS -> charger) against a fake charger over a real WebSocket."""

import asyncio
from contextlib import suppress
from typing import Any

import pytest
from ocpp.v16.enums import ConfigurationStatus, ResetStatus, ResetType

from fakes import CsmsHarness, fake_charger, wait_until
from ocpp_ops.csms.commands import (
    ChargerCallError,
    ChargerNotConnectedError,
    CommandTimeoutError,
)
from ocpp_ops.csms.handlers import CsmsChargePoint
from ocpp_ops.csms.registry import ChargerState, ConfigurationKey


def _charger(csms: CsmsHarness, charger_id: str = "CP_1") -> ChargerState:
    charger = csms.registry.get(charger_id)
    assert charger is not None, f"{charger_id} not in registry"
    return charger


# --- GetConfiguration -------------------------------------------------------------


async def test_get_all_configuration(csms: CsmsHarness) -> None:
    async with fake_charger(csms.url) as cp:
        await cp.boot()

        result = await csms.central_system.get_configuration("CP_1")

        assert {item.key: item.value for item in result.configuration} == cp.config
        assert result.unknown_keys == []


async def test_get_selected_configuration_with_unknown_key(csms: CsmsHarness) -> None:
    async with fake_charger(csms.url) as cp:
        await cp.boot()

        result = await csms.central_system.get_configuration(
            "CP_1", ["NumberOfConnectors", "NoSuchKey"]
        )

        assert result.configuration == [
            ConfigurationKey(key="NumberOfConnectors", readonly=True, value="2")
        ]
        assert result.unknown_keys == ["NoSuchKey"]


async def test_get_configuration_updates_known_config(csms: CsmsHarness) -> None:
    async with fake_charger(csms.url) as cp:
        await cp.boot()
        await csms.central_system.get_configuration("CP_1")
        assert set(_charger(csms).known_config) == set(cp.config)

        # The charger no longer knows a key: the next read forgets it.
        del cp.config["WebSocketPingInterval"]
        await csms.central_system.get_configuration("CP_1", ["WebSocketPingInterval"])

        known_config = _charger(csms).known_config
        assert "WebSocketPingInterval" not in known_config
        assert known_config["HeartbeatInterval"].value == "60"
        assert known_config["NumberOfConnectors"].readonly


# --- ChangeConfiguration ----------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("HeartbeatInterval", ConfigurationStatus.accepted),
        ("NumberOfConnectors", ConfigurationStatus.rejected),
        ("WebSocketPingInterval", ConfigurationStatus.reboot_required),
        ("NoSuchKey", ConfigurationStatus.not_supported),
    ],
)
async def test_change_configuration_returns_and_records_answer(
    csms: CsmsHarness, key: str, expected: ConfigurationStatus
) -> None:
    async with fake_charger(csms.url) as cp:
        await cp.boot()

        result = await csms.central_system.change_configuration("CP_1", key, "30")

        assert (result.key, result.value, result.status) == (key, "30", expected)
        change = _charger(csms).config_changes[key]
        assert (change.value, change.status, change.at) == ("30", expected, csms.clock())


async def test_accepted_change_does_not_touch_known_config(csms: CsmsHarness) -> None:
    async with fake_charger(csms.url) as cp:
        await cp.boot()
        await csms.central_system.get_configuration("CP_1", ["HeartbeatInterval"])

        await csms.central_system.change_configuration("CP_1", "HeartbeatInterval", "30")
        # known_config is what the charger last *reported*, not what we asked for.
        assert _charger(csms).known_config["HeartbeatInterval"].value == "60"

        await csms.central_system.get_configuration("CP_1", ["HeartbeatInterval"])
        assert _charger(csms).known_config["HeartbeatInterval"].value == "30"


# --- Reset ------------------------------------------------------------------------


@pytest.mark.parametrize("reset_type", [ResetType.soft, ResetType.hard])
async def test_reset(csms: CsmsHarness, reset_type: ResetType) -> None:
    async with fake_charger(csms.url) as cp:
        await cp.boot()

        result = await csms.central_system.reset("CP_1", reset_type)

        assert (result.type, result.status) == (reset_type, ResetStatus.accepted)
        assert cp.resets == [reset_type.value]


# --- Errors -----------------------------------------------------------------------


async def test_unknown_charger_raises(csms: CsmsHarness) -> None:
    with pytest.raises(ChargerNotConnectedError, match="'CP_404' is not connected"):
        await csms.central_system.reset("CP_404", ResetType.soft)


async def test_disconnected_charger_raises(csms: CsmsHarness) -> None:
    async with fake_charger(csms.url) as cp:
        await cp.boot()
        await cp.websocket.close()
    await wait_until(lambda: not _charger(csms).connected)

    with pytest.raises(ChargerNotConnectedError):
        await csms.central_system.get_configuration("CP_1")


async def test_silent_charger_times_out(csms: CsmsHarness) -> None:
    async with fake_charger(csms.url) as cp:
        await cp.boot()
        cp.mode = "silent"

        with pytest.raises(CommandTimeoutError) as timed_out:
            await csms.central_system.get_configuration("CP_1")

        assert (timed_out.value.charger_id, timed_out.value.action) == ("CP_1", "GetConfiguration")
        assert "HeartbeatInterval" not in _charger(csms).known_config


async def test_call_error_from_charger_raises(csms: CsmsHarness) -> None:
    async with fake_charger(csms.url) as cp:
        await cp.boot()
        cp.mode = "error"

        with pytest.raises(ChargerCallError) as failed:
            await csms.central_system.change_configuration("CP_1", "HeartbeatInterval", "30")

        assert failed.value.error_code == "InternalError"
        assert "HeartbeatInterval" not in _charger(csms).config_changes


async def test_commands_go_to_the_newest_connection(csms: CsmsHarness) -> None:
    async with fake_charger(csms.url, "CP_1") as old, fake_charger(csms.url, "CP_1") as new:
        await old.boot()
        await new.boot()

        await csms.central_system.reset("CP_1", ResetType.soft)
        assert (old.resets, new.resets) == ([], ["Soft"])

        await old.websocket.close()
        await wait_until(lambda: len(csms.registry.disconnect_calls) == 1)
        await csms.central_system.reset("CP_1", ResetType.hard)
        assert new.resets == ["Soft", "Hard"]


async def test_command_cancelled_as_the_answer_arrives_raises_cancelled(
    csms: CsmsHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On Python 3.11, ocpp's call() can return the answer of a cancelled command
    (CPython gh-86296). Recreated here on every version: the command must still raise."""
    original_call = CsmsChargePoint.call

    async def call_like_python_311(
        self: CsmsChargePoint, payload: Any, *args: Any, **kwargs: Any
    ) -> Any:
        response = await original_call(self, payload, *args, **kwargs)
        task = asyncio.current_task()
        assert task is not None
        task.cancel()  # cancelled just as the answer arrives...
        with suppress(asyncio.CancelledError):
            await asyncio.sleep(0)  # ...and the cancellation swallowed, as 3.11's wait_for does
        return response

    async with fake_charger(csms.url) as cp:
        await cp.boot()
        monkeypatch.setattr(CsmsChargePoint, "call", call_like_python_311)
        command = asyncio.create_task(csms.central_system.get_configuration("CP_1"))
        with pytest.raises(asyncio.CancelledError):
            await command
