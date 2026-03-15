"""Tests for polling watchdog and restart logic."""

# ruff: noqa: E402
import asyncio
import contextlib
import time
import unittest
from unittest.mock import AsyncMock, Mock, PropertyMock, patch
from pathlib import Path
import sys
import types
import os

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

_ORIGINAL_PROJECT_ROOT = os.environ.get("PROJECT_ROOT")
_ORIGINAL_CONFIG_MODULE = sys.modules.get("telegram_bot.utils.config")
os.environ["PROJECT_ROOT"] = str(Path(__file__).resolve().parents[1])

from pathlib import Path as _Path

config_module = types.ModuleType("telegram_bot.utils.config")
setattr(
    config_module,
    "config",
    types.SimpleNamespace(
        telegram_bot_token="test_token",
        network_retry_attempts=3,
        network_retry_delay=5,
        polling_timeout=30,
        bot_data_dir=_Path("/tmp/test_bot"),
        logs_dir=_Path("/tmp/test_bot/logs"),
        session_store_path=_Path("/tmp/test_bot/sessions.json"),
        allowed_user_ids=[],
        draft_update_min_chars=150,
        draft_update_interval=1.0,
        ffmpeg_path=None,
        claude_cli_path=None,
        claude_settings_path=_Path.home() / ".claude" / "settings.json",
    ),
)
sys.modules["telegram_bot.utils.config"] = config_module

import telegram.error

sys.modules.pop("telegram_bot.core.bot", None)
import telegram_bot.core.bot as bot_module

TelegramBot = bot_module.TelegramBot
_PollingRestart = bot_module._PollingRestart

if _ORIGINAL_PROJECT_ROOT is None:
    os.environ.pop("PROJECT_ROOT", None)
else:
    os.environ["PROJECT_ROOT"] = _ORIGINAL_PROJECT_ROOT

if _ORIGINAL_CONFIG_MODULE is None:
    sys.modules.pop("telegram_bot.utils.config", None)
else:
    sys.modules["telegram_bot.utils.config"] = _ORIGINAL_CONFIG_MODULE

sys.modules.pop("telegram_bot.core.bot", None)


class TestPollingWatchdog(unittest.TestCase):
    """Test watchdog detection and polling restart."""

    def setUp(self):
        self.bot = TelegramBot()

    def test_watchdog_healthy_api_no_restart(self):
        """Watchdog does not restart when API is reachable."""
        stop_event = asyncio.Event()
        mock_app = Mock()
        mock_bot = Mock()
        mock_bot.get_me = AsyncMock(return_value=True)
        mock_app.bot = mock_bot

        mock_updater = Mock()
        type(mock_updater).running = PropertyMock(return_value=True)
        mock_app.updater = mock_updater

        self.bot.application = mock_app
        self.bot._WATCHDOG_INTERVAL = 0.05  # speed up test

        async def run():
            task = asyncio.create_task(self.bot._polling_watchdog(stop_event))
            await asyncio.sleep(0.2)
            stop_event.set()
            await task

        asyncio.run(run())

        self.assertTrue(mock_bot.get_me.called)
        self.assertFalse(self.bot._restart_requested)
        self.assertIsNone(self.bot._api_unreachable_since)
        self.assertEqual(self.bot._api_failure_count, 0)

    def test_watchdog_requests_restart_after_threshold(self):
        """Watchdog requests restart and stops updater after threshold is exceeded."""
        stop_event = asyncio.Event()
        mock_app = Mock()
        mock_bot = Mock()
        mock_bot.get_me = AsyncMock(side_effect=Exception("timeout"))
        mock_app.bot = mock_bot

        mock_updater = Mock()
        updater_state = {"running": True}
        type(mock_updater).running = PropertyMock(
            side_effect=lambda: updater_state["running"]
        )

        async def stop_updater():
            updater_state["running"] = False

        mock_updater.stop = AsyncMock(side_effect=stop_updater)
        mock_app.updater = mock_updater

        self.bot.application = mock_app
        self.bot._WATCHDOG_INTERVAL = 0.05
        self.bot._NETWORK_FAILURE_THRESHOLD = 0.1

        async def run():
            with patch.object(
                self.bot,
                "_arm_hard_exit",
                side_effect=lambda *args, **kwargs: setattr(
                    self.bot, "_hard_exit_armed", True
                ),
            ):
                task = asyncio.create_task(self.bot._polling_watchdog(stop_event))
                await asyncio.sleep(0.2)
                stop_event.set()
                await task
                if self.bot._updater_stop_task:
                    await self.bot._updater_stop_task

        asyncio.run(run())

        mock_updater.stop.assert_awaited()
        self.assertTrue(self.bot._restart_requested)
        self.assertEqual(
            self.bot._restart_reason,
            "telegram api unreachable for 1s via watchdog",
        )
        self.assertTrue(self.bot._hard_exit_armed)

    def test_polling_and_watchdog_share_outage_window(self):
        """Polling error callback and watchdog reuse the same outage window."""
        stop_event = asyncio.Event()
        mock_app = Mock()
        mock_bot = Mock()
        mock_bot.get_me = AsyncMock(side_effect=Exception("timeout"))
        mock_app.bot = mock_bot

        mock_updater = Mock()
        updater_state = {"running": True}
        type(mock_updater).running = PropertyMock(
            side_effect=lambda: updater_state["running"]
        )

        async def stop_updater():
            updater_state["running"] = False

        mock_updater.stop = AsyncMock(side_effect=stop_updater)
        mock_app.updater = mock_updater

        self.bot.application = mock_app
        self.bot._WATCHDOG_INTERVAL = 0.01
        self.bot._NETWORK_FAILURE_THRESHOLD = 2
        self.bot._polling_error_callback(telegram.error.NetworkError("down"))
        initial_outage_since = self.bot._api_unreachable_since
        self.assertEqual(self.bot._api_unreachable_since, initial_outage_since)
        self.assertEqual(self.bot._api_failure_count, 1)
        self.assertFalse(self.bot._restart_requested)

        async def run():
            with (
                patch.object(
                    self.bot,
                    "_api_outage_seconds",
                    return_value=2,
                ),
                patch.object(
                    self.bot,
                    "_arm_hard_exit",
                    side_effect=lambda *args, **kwargs: setattr(
                        self.bot, "_hard_exit_armed", True
                    ),
                ),
            ):
                task = asyncio.create_task(self.bot._polling_watchdog(stop_event))
                await asyncio.sleep(0.03)
                stop_event.set()
                await task
                if self.bot._updater_stop_task:
                    await self.bot._updater_stop_task

        asyncio.run(run())

        self.assertEqual(self.bot._api_unreachable_since, initial_outage_since)
        self.assertGreaterEqual(self.bot._api_failure_count, 2)
        self.assertTrue(self.bot._restart_requested)
        self.assertEqual(
            self.bot._restart_reason,
            "telegram api unreachable for 2s via watchdog",
        )

    def test_watchdog_recovery_clears_shared_outage_state(self):
        """Watchdog clears outage state after polling-side failures recover."""
        stop_event = asyncio.Event()
        mock_app = Mock()
        mock_bot = Mock()
        mock_bot.get_me = AsyncMock(return_value=True)
        mock_app.bot = mock_bot

        mock_updater = Mock()
        type(mock_updater).running = PropertyMock(return_value=True)
        mock_app.updater = mock_updater

        self.bot.application = mock_app
        self.bot._WATCHDOG_INTERVAL = 0.05
        self.bot._polling_error_callback(telegram.error.NetworkError("down"))

        async def run():
            task = asyncio.create_task(self.bot._polling_watchdog(stop_event))
            await asyncio.sleep(0.2)
            stop_event.set()
            await task

        asyncio.run(run())

        self.assertIsNone(self.bot._api_unreachable_since)
        self.assertEqual(self.bot._api_failure_count, 0)

    def test_polling_conflict_requests_process_exit(self):
        """Polling conflict requests a fatal process exit."""
        mock_app = Mock()
        mock_updater = Mock()
        type(mock_updater).running = PropertyMock(return_value=True)
        mock_app.updater = mock_updater
        self.bot.application = mock_app

        with patch.object(
            self.bot,
            "_arm_hard_exit",
            side_effect=lambda *args, **kwargs: setattr(
                self.bot, "_hard_exit_armed", True
            ),
        ):
            self.bot._polling_error_callback(telegram.error.Conflict("duplicate"))

        self.assertEqual(
            self.bot._fatal_exit_message,
            "Another bot instance started polling with the same token.",
        )
        self.assertFalse(self.bot._restart_requested)
        self.assertTrue(self.bot._hard_exit_armed)


class TestWaitForPollingExit(unittest.TestCase):
    """Test _wait_for_polling_exit detects terminal states."""

    def setUp(self):
        self.bot = TelegramBot()

    def test_fatal_exit_message_raises_system_exit(self):
        """Fatal exit requests break the wait loop immediately."""
        stop_event = asyncio.Event()
        mock_app = Mock()
        mock_updater = Mock()
        type(mock_updater).running = PropertyMock(return_value=True)
        mock_app.updater = mock_updater

        self.bot.application = mock_app
        self.bot._fatal_exit_message = "fatal"

        async def run():
            with self.assertRaises(SystemExit) as ctx:
                await self.bot._wait_for_polling_exit(stop_event)
            self.assertEqual(str(ctx.exception), "fatal")

        asyncio.run(run())

    def test_unexpected_exit_triggers_restart(self):
        """Polling exiting unexpectedly triggers _PollingRestart."""
        stop_event = asyncio.Event()
        mock_app = Mock()
        mock_updater = Mock()
        type(mock_updater).running = PropertyMock(return_value=False)
        mock_app.updater = mock_updater

        self.bot.application = mock_app

        async def run():
            with patch.object(
                self.bot,
                "_arm_hard_exit",
                side_effect=lambda *args, **kwargs: setattr(
                    self.bot, "_hard_exit_armed", True
                ),
            ):
                with self.assertRaises(_PollingRestart):
                    await self.bot._wait_for_polling_exit(stop_event)

        asyncio.run(run())
        self.assertTrue(self.bot._restart_requested)
        self.assertEqual(self.bot._restart_reason, "polling exited unexpectedly")
        self.assertTrue(self.bot._hard_exit_armed)
        self.assertIsNone(self.bot._updater_stop_task)

    def test_watchdog_task_crash_triggers_restart(self):
        """A crashed watchdog task should force a polling restart."""
        stop_event = asyncio.Event()
        mock_app = Mock()
        mock_updater = Mock()
        updater_state = {"running": True}
        type(mock_updater).running = PropertyMock(
            side_effect=lambda: updater_state["running"]
        )

        async def stop_updater():
            updater_state["running"] = False

        mock_updater.stop = AsyncMock(side_effect=stop_updater)
        mock_app.updater = mock_updater
        self.bot.application = mock_app

        async def run():
            async def crash_watchdog():
                raise RuntimeError("watchdog boom")

            watchdog_task = asyncio.create_task(crash_watchdog())
            await asyncio.sleep(0)
            with patch.object(
                self.bot,
                "_arm_hard_exit",
                side_effect=lambda *args, **kwargs: setattr(
                    self.bot, "_hard_exit_armed", True
                ),
            ):
                with self.assertRaises(_PollingRestart):
                    await self.bot._wait_for_polling_exit(stop_event, watchdog_task)
                if self.bot._updater_stop_task:
                    await self.bot._updater_stop_task

        asyncio.run(run())
        self.assertTrue(self.bot._restart_requested)
        self.assertIn("polling watchdog crashed", self.bot._restart_reason)
        self.assertTrue(self.bot._hard_exit_armed)

    def test_watchdog_stall_triggers_restart(self):
        """A stalled watchdog should not leave the process idling forever."""
        stop_event = asyncio.Event()
        mock_app = Mock()
        mock_updater = Mock()
        updater_state = {"running": True}
        type(mock_updater).running = PropertyMock(
            side_effect=lambda: updater_state["running"]
        )

        async def stop_updater():
            updater_state["running"] = False

        mock_updater.stop = AsyncMock(side_effect=stop_updater)
        mock_app.updater = mock_updater
        self.bot.application = mock_app
        self.bot._watchdog_last_progress_at = time.monotonic() - 999
        self.bot._WATCHDOG_INTERVAL = 60
        self.bot._WATCHDOG_API_TIMEOUT = 10
        self.bot._WATCHDOG_STALL_GRACE = 15

        async def run():
            watchdog_task = asyncio.create_task(asyncio.sleep(999))
            with patch.object(
                self.bot,
                "_arm_hard_exit",
                side_effect=lambda *args, **kwargs: setattr(
                    self.bot, "_hard_exit_armed", True
                ),
            ):
                with self.assertRaises(_PollingRestart):
                    await self.bot._wait_for_polling_exit(stop_event, watchdog_task)
                if self.bot._updater_stop_task:
                    await self.bot._updater_stop_task
                watchdog_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await watchdog_task

        asyncio.run(run())
        self.assertTrue(self.bot._restart_requested)
        self.assertTrue(
            self.bot._restart_reason.startswith("polling watchdog stalled for ")
        )
        self.assertTrue(self.bot._hard_exit_armed)

    def test_polling_activity_stall_triggers_restart(self):
        """Stalled getUpdates activity should force polling restart."""
        stop_event = asyncio.Event()
        mock_app = Mock()
        mock_updater = Mock()
        updater_state = {"running": True}
        type(mock_updater).running = PropertyMock(
            side_effect=lambda: updater_state["running"]
        )

        async def stop_updater():
            updater_state["running"] = False

        mock_updater.stop = AsyncMock(side_effect=stop_updater)
        mock_app.updater = mock_updater
        self.bot.application = mock_app
        self.bot._polling_last_activity_at = time.monotonic() - 999
        self.bot._POLLING_ACTIVITY_STALL_TIMEOUT = 90

        async def run():
            watchdog_task = asyncio.create_task(asyncio.sleep(999))
            with patch.object(
                self.bot,
                "_arm_hard_exit",
                side_effect=lambda *args, **kwargs: setattr(
                    self.bot, "_hard_exit_armed", True
                ),
            ):
                with self.assertRaises(_PollingRestart):
                    await self.bot._wait_for_polling_exit(stop_event, watchdog_task)
                if self.bot._updater_stop_task:
                    await self.bot._updater_stop_task
                watchdog_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await watchdog_task

        asyncio.run(run())
        self.assertTrue(self.bot._restart_requested)
        self.assertTrue(self.bot._restart_reason.startswith("getUpdates inactive for "))
        self.assertTrue(self.bot._hard_exit_armed)

    def test_stop_event_exits_cleanly(self):
        """Setting stop_event exits _wait_for_polling_exit without _PollingRestart."""
        stop_event = asyncio.Event()
        mock_app = Mock()
        mock_updater = Mock()
        type(mock_updater).running = PropertyMock(return_value=True)
        mock_app.updater = mock_updater

        self.bot.application = mock_app

        async def run():
            stop_event.set()
            await self.bot._wait_for_polling_exit(stop_event)

        asyncio.run(run())


class TestGracefulShutdown(unittest.TestCase):
    """Test _graceful_shutdown cleans up properly."""

    def setUp(self):
        self.bot = TelegramBot()

    def test_shutdown_stops_all_components(self):
        """Graceful shutdown stops updater, app, and calls shutdown."""
        mock_app = Mock()
        mock_updater = Mock()
        type(mock_updater).running = PropertyMock(return_value=True)
        mock_updater.stop = AsyncMock()
        mock_app.updater = mock_updater
        type(mock_app).running = PropertyMock(return_value=True)
        mock_app.stop = AsyncMock()
        mock_app.shutdown = AsyncMock()

        self.bot.application = mock_app

        asyncio.run(self.bot._graceful_shutdown())

        mock_updater.stop.assert_awaited_once()
        mock_app.stop.assert_awaited_once()
        mock_app.shutdown.assert_awaited_once()
        self.assertIsNone(self.bot.application)

    def test_shutdown_noop_when_no_application(self):
        """Graceful shutdown is a no-op when application is None."""
        self.bot.application = None
        asyncio.run(self.bot._graceful_shutdown())
        self.assertIsNone(self.bot.application)

    def test_shutdown_handles_errors(self):
        """Graceful shutdown does not raise even if components fail."""
        mock_app = Mock()
        mock_updater = Mock()
        type(mock_updater).running = PropertyMock(return_value=True)
        mock_updater.stop = AsyncMock(side_effect=RuntimeError("stop failed"))
        mock_app.updater = mock_updater
        type(mock_app).running = PropertyMock(return_value=True)
        mock_app.stop = AsyncMock()
        mock_app.shutdown = AsyncMock()

        self.bot.application = mock_app

        asyncio.run(self.bot._graceful_shutdown())
        self.assertIsNone(self.bot.application)

    def test_shutdown_timeout_forces_exit(self):
        """Shutdown timeouts force process exit instead of hanging forever."""
        mock_app = Mock()
        mock_updater = Mock()
        type(mock_updater).running = PropertyMock(return_value=True)

        async def slow_stop():
            await asyncio.sleep(999)

        mock_updater.stop = slow_stop
        mock_app.updater = mock_updater
        type(mock_app).running = PropertyMock(return_value=False)
        mock_app.shutdown = AsyncMock()

        self.bot.application = mock_app
        self.bot._UPDATER_STOP_TIMEOUT = 0.01

        async def run():
            with patch("os._exit") as mock_exit:
                mock_exit.side_effect = SystemExit(1)
                with self.assertRaises(SystemExit):
                    await self.bot._graceful_shutdown()
                mock_exit.assert_called_once_with(1)

        asyncio.run(run())

    def test_pending_updater_stop_task_timeout_forces_exit(self):
        """Waiting on a pending stop-updater task should not hang shutdown."""
        mock_app = Mock()
        mock_updater = Mock()
        type(mock_updater).running = PropertyMock(return_value=False)
        mock_app.updater = mock_updater
        type(mock_app).running = PropertyMock(return_value=False)
        mock_app.shutdown = AsyncMock()

        self.bot.application = mock_app
        self.bot._UPDATER_STOP_TIMEOUT = 0.01

        async def run():
            self.bot._updater_stop_task = asyncio.create_task(asyncio.sleep(999))
            with patch("os._exit") as mock_exit:
                mock_exit.side_effect = SystemExit(1)
                with self.assertRaises(SystemExit):
                    await self.bot._graceful_shutdown()
                mock_exit.assert_called_once_with(1)

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
