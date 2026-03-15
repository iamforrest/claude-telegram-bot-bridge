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

_ORIGINAL_PROJECT_ROOT = os.environ.get("PROJECT_ROOT")
_ORIGINAL_CONFIG_MODULE = sys.modules.get("telegram_bot.utils.config")
# Set PROJECT_ROOT before importing bot modules
os.environ["PROJECT_ROOT"] = str(Path(__file__).resolve().parents[1])

# Mock config module
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
from telegram import Update

sys.modules.pop("telegram_bot.core.bot", None)
import telegram_bot.core.bot as bot_module

TelegramBot = bot_module.TelegramBot

if _ORIGINAL_PROJECT_ROOT is None:
    os.environ.pop("PROJECT_ROOT", None)
else:
    os.environ["PROJECT_ROOT"] = _ORIGINAL_PROJECT_ROOT

if _ORIGINAL_CONFIG_MODULE is None:
    sys.modules.pop("telegram_bot.utils.config", None)
else:
    sys.modules["telegram_bot.utils.config"] = _ORIGINAL_CONFIG_MODULE

sys.modules.pop("telegram_bot.core.bot", None)


class TestConnectionResilience(unittest.TestCase):
    """Test connection resilience and retry logic."""

    def setUp(self):
        """Set up test fixtures."""
        self.bot = TelegramBot()

    @patch.object(bot_module, "Application")
    def test_builder_configures_timeouts(self, mock_app_class):
        """Test that Application.builder() tracks getUpdates activity with custom request."""
        mock_builder = Mock()
        mock_app_class.builder.return_value = mock_builder

        mock_builder.token.return_value = mock_builder
        mock_builder.concurrent_updates.return_value = mock_builder
        mock_builder.get_updates_request.return_value = mock_builder
        mock_builder.build.return_value = Mock()

        self.bot.build()

        mock_builder.get_updates_request.assert_called_once()
        tracked_request = mock_builder.get_updates_request.call_args.args[0]
        self.assertIsInstance(tracked_request, bot_module._ActivityTrackingRequest)
        self.assertIsInstance(tracked_request._wrapped, bot_module.HTTPXRequest)
        self.assertEqual(
            tracked_request._wrapped.read_timeout,
            self.bot._GET_UPDATES_READ_TIMEOUT,
        )

    def test_activity_tracking_request_records_start_and_finish(self):
        """The request wrapper should emit activity before and after getUpdates."""

        class DummyRequest(bot_module.BaseRequest):
            @property
            def read_timeout(self):
                return 1.0

            async def initialize(self):
                return None

            async def shutdown(self):
                return None

            async def do_request(
                self,
                url,
                method,
                request_data=None,
                read_timeout=bot_module.BaseRequest.DEFAULT_NONE,
                write_timeout=bot_module.BaseRequest.DEFAULT_NONE,
                connect_timeout=bot_module.BaseRequest.DEFAULT_NONE,
                pool_timeout=bot_module.BaseRequest.DEFAULT_NONE,
            ):
                return 200, b"{}"

        activity_ticks: list[str] = []
        tracked_request = bot_module._ActivityTrackingRequest(
            DummyRequest(),
            lambda: activity_ticks.append("tick"),
            label="getUpdates",
        )

        async def run():
            await tracked_request.do_request("https://example.com", "POST")

        asyncio.run(run())
        self.assertEqual(activity_ticks, ["tick", "tick"])

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

    def test_start_polling_registers_error_callback(self):
        """Polling startup attaches the supervisor error callback."""
        mock_app = Mock()
        mock_app.initialize = AsyncMock()
        mock_app.start = AsyncMock()

        mock_updater = Mock()
        updater_state = {"running": True}
        type(mock_updater).running = PropertyMock(
            side_effect=lambda: updater_state["running"]
        )

        async def stop_updater():
            updater_state["running"] = False

        mock_updater.start_polling = AsyncMock()
        mock_updater.stop = AsyncMock(side_effect=stop_updater)
        mock_app.updater = mock_updater

        type(mock_app).running = PropertyMock(return_value=True)
        mock_app.stop = AsyncMock()
        mock_app.shutdown = AsyncMock()
        mock_app.bot = Mock()

        self.bot.application = mock_app
        self.bot.build = Mock()
        self.bot._on_ready = AsyncMock()
        self.bot._wait_for_polling_exit = AsyncMock(side_effect=SystemExit("stop"))

        with self.assertRaises(SystemExit):
            self.bot.run()

        _, kwargs = mock_updater.start_polling.call_args
        self.assertEqual(kwargs["allowed_updates"], Update.ALL_TYPES)
        self.assertFalse(kwargs["drop_pending_updates"])
        error_callback = kwargs["error_callback"]
        self.assertIs(error_callback.__self__, self.bot)
        self.assertIs(error_callback.__func__, TelegramBot._polling_error_callback)

    @patch("time.time")
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

        with patch.object(self.bot, "_arm_hard_exit"):
            with self.assertRaises(SystemExit) as ctx:
                self.bot.run()

        self.assertIn("Giving up", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
