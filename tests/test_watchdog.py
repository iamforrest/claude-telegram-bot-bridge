"""Tests for polling watchdog and restart logic."""
# ruff: noqa: E402
import asyncio
import unittest
from unittest.mock import AsyncMock, Mock, PropertyMock, patch
from pathlib import Path
import sys
import types
import os

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

os.environ["PROJECT_ROOT"] = str(Path(__file__).resolve().parents[1])

from pathlib import Path as _Path
config_module = types.ModuleType("telegram_bot.utils.config")
config_module.config = types.SimpleNamespace(
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
)
sys.modules["telegram_bot.utils.config"] = config_module

from telegram_bot.core.bot import TelegramBot, _PollingRestart


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

        # get_me was called at least once and no exception raised
        self.assertTrue(mock_bot.get_me.called)

    def test_watchdog_triggers_restart_after_threshold(self):
        """Watchdog triggers _PollingRestart after consecutive failures exceed threshold."""
        stop_event = asyncio.Event()
        mock_app = Mock()
        mock_bot = Mock()
        mock_bot.get_me = AsyncMock(side_effect=Exception("timeout"))
        mock_app.bot = mock_bot

        mock_updater = Mock()
        type(mock_updater).running = PropertyMock(return_value=True)
        mock_updater.stop = AsyncMock()
        mock_app.updater = mock_updater

        self.bot.application = mock_app
        self.bot._WATCHDOG_INTERVAL = 0.05
        self.bot._NETWORK_FAILURE_THRESHOLD = 0.1  # trigger after ~2 failures

        async def run():
            with self.assertRaises(_PollingRestart):
                await self.bot._polling_watchdog(stop_event)

        asyncio.run(run())

        mock_updater.stop.assert_awaited()

    def test_watchdog_recovery_resets_counter(self):
        """Watchdog resets failure counter when API becomes reachable again."""
        stop_event = asyncio.Event()
        mock_app = Mock()
        mock_bot = Mock()

        call_count = 0
        async def mock_get_me():
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                raise Exception("timeout")
            return True

        mock_bot.get_me = mock_get_me
        mock_app.bot = mock_bot

        mock_updater = Mock()
        type(mock_updater).running = PropertyMock(return_value=True)
        mock_app.updater = mock_updater

        self.bot.application = mock_app
        self.bot._WATCHDOG_INTERVAL = 0.05
        self.bot._NETWORK_FAILURE_THRESHOLD = 999  # high threshold so no restart

        async def run():
            task = asyncio.create_task(self.bot._polling_watchdog(stop_event))
            await asyncio.sleep(0.3)
            stop_event.set()
            await task

        asyncio.run(run())

        # After failures then success, no _PollingRestart was raised
        self.assertGreater(call_count, 2)

    def test_watchdog_updater_stop_timeout_causes_exit(self):
        """If updater.stop() times out, os._exit(1) is called."""
        stop_event = asyncio.Event()
        mock_app = Mock()
        mock_bot = Mock()
        mock_bot.get_me = AsyncMock(side_effect=Exception("timeout"))
        mock_app.bot = mock_bot

        mock_updater = Mock()
        type(mock_updater).running = PropertyMock(return_value=True)

        async def slow_stop():
            await asyncio.sleep(999)

        mock_updater.stop = slow_stop
        mock_app.updater = mock_updater

        self.bot.application = mock_app
        self.bot._WATCHDOG_INTERVAL = 0.01
        self.bot._NETWORK_FAILURE_THRESHOLD = 0.01

        async def run():
            with patch('os._exit') as mock_exit:
                mock_exit.side_effect = SystemExit(1)
                with self.assertRaises(SystemExit):
                    await self.bot._polling_watchdog(stop_event)
                mock_exit.assert_called_once_with(1)

        asyncio.run(run())


class TestWaitForPollingExit(unittest.TestCase):
    """Test _wait_for_polling_exit detects unexpected exits."""

    def setUp(self):
        self.bot = TelegramBot()

    def test_unexpected_exit_triggers_restart(self):
        """Polling exiting unexpectedly triggers _PollingRestart."""
        stop_event = asyncio.Event()
        mock_app = Mock()
        mock_updater = Mock()
        type(mock_updater).running = PropertyMock(return_value=False)
        mock_app.updater = mock_updater

        self.bot.application = mock_app

        async def run():
            with self.assertRaises(_PollingRestart):
                await self.bot._wait_for_polling_exit(stop_event)

        asyncio.run(run())

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


if __name__ == '__main__':
    unittest.main()
