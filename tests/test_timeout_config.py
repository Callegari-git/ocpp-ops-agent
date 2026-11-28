"""The per-test timeout (pytest-timeout) is on, and a hung test fails clearly."""

import pytest

pytest_plugins = ["pytester"]


def test_global_timeout_is_configured(pytestconfig: pytest.Config) -> None:
    assert pytestconfig.getini("timeout") == "30"
    assert pytestconfig.getini("timeout_method") == "signal"


def test_a_hung_async_test_fails_and_the_run_goes_on(pytester: pytest.Pytester) -> None:
    """The kind of hang we have had: a task stuck after swallowing its cancellation.
    It must fail with pytest-timeout's message, and the next test must still run."""
    pytester.makepyfile(
        """
        import asyncio
        from contextlib import suppress

        async def test_stuck_task():
            async def stubborn():
                with suppress(asyncio.CancelledError):
                    await asyncio.Event().wait()
                await asyncio.Event().wait()

            task = asyncio.create_task(stubborn())
            await asyncio.sleep(0)
            task.cancel()
            await task

        async def test_after():
            pass
        """
    )
    result = pytester.runpytest_subprocess(
        "-p",
        "no:cacheprovider",
        "-o",
        "asyncio_mode=auto",
        "-o",
        "asyncio_default_fixture_loop_scope=function",
        "--timeout=1",
        "--timeout-method=signal",
    )
    result.assert_outcomes(failed=1, passed=1)
    result.stdout.fnmatch_lines(
        [
            "E * Failed: Timeout (>1.0s) from pytest-timeout.",
            "FAILED *::test_stuck_task*",
        ]
    )
