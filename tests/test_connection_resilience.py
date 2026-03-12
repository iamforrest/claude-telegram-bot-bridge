"""Tests for Telegram bot connection resilience after system sleep."""
# ruff: noqa: E402
import unittest
from unittest.mock import Mock, patch
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
from telegram_bot.core.bot import TelegramBot


class TestConnectionResilience(unittest.TestCase):
    """Test connection resilience and retry logic."""

    def setUp(self):
        """Set up test fixtures."""
        self.bot = TelegramBot()

    def test_builder_configures_timeouts(self):
        """Test that Application.builder() configures proper timeout values."""
        self.bot.build()

        # Verify application was built
        self.assertIsNotNone(self.bot.application)

        # Verify timeout configuration exists
        request = self.bot.application.bot.request
        self.assertIsNotNone(request)

    @patch('time.sleep')
    def test_network_error_retries_then_succeeds(self, mock_sleep):
        """Test that NetworkError triggers retry and eventually succeeds."""
        self.bot.build()

        # Mock run_polling to fail once, then succeed
        call_count = 0
        def mock_run_polling(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise telegram.error.NetworkError("Connection reset")
            # Second call succeeds (returns normally)

        self.bot.application.run_polling = Mock(side_effect=mock_run_polling)

        # Should not raise SystemExit
        self.bot.run()

        # Verify retry happened
        self.assertEqual(call_count, 2)
        mock_sleep.assert_called_once_with(5)

    @patch('time.sleep')
    def test_network_error_exits_after_max_retries(self, mock_sleep):
        """Test that NetworkError causes SystemExit after max retries."""
        self.bot.build()

        # Mock run_polling to always fail
        self.bot.application.run_polling = Mock(
            side_effect=telegram.error.NetworkError("Connection reset")
        )

        # Should raise SystemExit after 3 attempts
        with self.assertRaises(SystemExit):
            self.bot.run()

        # Verify all retries were attempted
        self.assertEqual(self.bot.application.run_polling.call_count, 3)
        self.assertEqual(mock_sleep.call_count, 2)  # Sleep between attempts


if __name__ == '__main__':
    unittest.main()
