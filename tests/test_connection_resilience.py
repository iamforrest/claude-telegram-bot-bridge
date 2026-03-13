"""Tests for Telegram bot connection resilience after system sleep."""
# ruff: noqa: E402
import asyncio
import unittest
from unittest.mock import AsyncMock, Mock, patch, PropertyMock
from pathlib import Path
import sys
import types
import os

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# Set PROJECT_ROOT before importing bot modules
os.environ["PROJECT_ROOT"] = str(Path(__file__).resolve().parents[1])

# Mock config module
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

import telegram.error
from telegram_bot.core.bot import TelegramBot, _PollingRestart


class TestConnectionResilience(unittest.TestCase):
    """Test connection resilience and retry logic."""

    def setUp(self):
        """Set up test fixtures."""
        self.bot = TelegramBot()

    @patch('telegram_bot.core.bot.Application')
    def test_builder_configures_timeouts(self, mock_app_class):
        """Test that Application.builder() configures proper timeout values."""
        mock_builder = Mock()
        mock_app_class.builder.return_value = mock_builder

        mock_builder.token.return_value = mock_builder
        mock_builder.concurrent_updates.return_value = mock_builder
        mock_builder.get_updates_read_timeout.return_value = mock_builder
        mock_builder.get_updates_connect_timeout.return_value = mock_builder
        mock_builder.get_updates_pool_timeout.return_value = mock_builder
        mock_builder.build.return_value = Mock()

        self.bot.build()

        mock_builder.get_updates_read_timeout.assert_called_once_with(30)
        mock_builder.get_updates_connect_timeout.assert_called_once_with(10)
        mock_builder.get_updates_pool_timeout.assert_called_once_with(5)

    def test_invalid_token_raises_system_exit(self):
        """Test that InvalidToken during initialize raises SystemExit."""
        mock_app = Mock()
        mock_app.initialize = AsyncMock(
            side_effect=telegram.error.InvalidToken("bad token")
        )

        self.bot.application = mock_app
        self.bot.build = Mock()

        with self.assertRaises(SystemExit):
            self.bot.run()

    def test_conflict_raises_system_exit(self):
        """Test that Conflict during initialize raises SystemExit."""
        mock_app = Mock()
        mock_app.initialize = AsyncMock(
            side_effect=telegram.error.Conflict("duplicate")
        )

        self.bot.application = mock_app
        self.bot.build = Mock()

        with self.assertRaises(SystemExit):
            self.bot.run()

    @patch('time.time')
    def test_rapid_restart_triggers_system_exit(self, mock_time):
        """Test that repeated rapid polling restarts trigger SystemExit."""
        # Each _run_async iteration: time() at start, time() in _PollingRestart handler
        # Return incrementing values so uptime is always 1s (< MIN_UPTIME=30)
        mock_time.side_effect = list(range(100))

        mock_app = Mock()
        mock_app.initialize = AsyncMock()
        mock_app.start = AsyncMock()

        mock_updater = Mock()
        mock_updater.start_polling = AsyncMock()
        # Polling immediately "exits" to trigger _PollingRestart
        type(mock_updater).running = PropertyMock(return_value=False)
        mock_updater.stop = AsyncMock()
        mock_app.updater = mock_updater
        type(mock_app).running = PropertyMock(return_value=True)
        mock_app.stop = AsyncMock()
        mock_app.shutdown = AsyncMock()
        mock_app.bot = Mock()

        self.bot._on_ready = AsyncMock()

        build_count = 0
        def mock_build():
            nonlocal build_count
            build_count += 1
            self.bot.application = mock_app

        self.bot.build = mock_build
        self.bot.application = mock_app

        with self.assertRaises(SystemExit) as ctx:
            self.bot.run()

        self.assertIn("Giving up", str(ctx.exception))


if __name__ == '__main__':
    unittest.main()
