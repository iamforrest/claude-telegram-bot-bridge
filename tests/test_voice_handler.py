# ruff: noqa: E402

import asyncio
import sys
import types
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


config_module = types.ModuleType("telegram_bot.utils.config")
config_module.config = SimpleNamespace(
    telegram_bot_token="test-token",
    allowed_user_ids=[],
    claude_settings_path=Path("/tmp/settings.json"),
    max_voice_duration=300,
    bot_data_dir=Path("/tmp/telegram-bot-data"),
    openai_api_key="test-key",
    openai_base_url=None,
    whisper_model="whisper-1",
    ffmpeg_path="ffmpeg",
    draft_update_min_chars=20,
    draft_update_interval=0.1,
)
sys.modules["telegram_bot.utils.config"] = config_module


session_module = types.ModuleType("telegram_bot.session.manager")


class _SessionManager:
    async def get_session(self, user_id):
        del user_id
        return {}

    async def update_session(self, user_id, data):
        del user_id, data
        return None

    async def get_pending_question(self, user_id):
        del user_id
        return None

    async def clear_pending_question(self, user_id):
        del user_id
        return None

    async def clear_approve_all(self, user_id):
        del user_id
        return None


session_module.session_manager = _SessionManager()
sys.modules["telegram_bot.session.manager"] = session_module


project_chat_module = types.ModuleType("telegram_bot.core.project_chat")


class _ChatResponse:
    def __init__(self, content="", session_id=None, has_options=False, streamed=False):
        self.content = content
        self.session_id = session_id
        self.has_options = has_options
        self.streamed = streamed


class _ProjectChatHandler:
    async def process_message(self, **kwargs):
        del kwargs
        return _ChatResponse(content="ok")

    async def stop(self, user_id):
        del user_id
        return False

    async def cancel_user_streaming(self, user_id):
        del user_id
        return False

    def list_sessions(self, limit=10):
        del limit
        return []

    def get_session_last_assistant_message(self, session_id):
        del session_id
        return None


project_chat_module.project_chat_handler = _ProjectChatHandler()
project_chat_module.ChatResponse = _ChatResponse
project_chat_module.PROJECT_ROOT = Path("/tmp")
sys.modules["telegram_bot.core.project_chat"] = project_chat_module


chat_logger_module = types.ModuleType("telegram_bot.utils.chat_logger")
chat_logger_module.log_debug = lambda *args, **kwargs: None
sys.modules["telegram_bot.utils.chat_logger"] = chat_logger_module


permission_module = types.ModuleType("claude_code_sdk.types")


class _PermissionResultAllow:
    pass


class _PermissionResultDeny:
    def __init__(self, message=""):
        self.message = message


permission_module.PermissionResultAllow = _PermissionResultAllow
permission_module.PermissionResultDeny = _PermissionResultDeny
sys.modules["claude_code_sdk.types"] = permission_module


from telegram_bot.core.bot import TelegramBot


class VoiceHandlerHelperTests(unittest.IsolatedAsyncioTestCase):
    def test_resolve_voice_extension(self):
        bot = TelegramBot()
        self.assertEqual(bot._resolve_voice_extension("audio/ogg"), "ogg")
        self.assertEqual(bot._resolve_voice_extension("audio/amr"), "amr")
        self.assertEqual(bot._resolve_voice_extension(None), "ogg")

    def test_build_voice_file_name(self):
        bot = TelegramBot()
        name = bot._build_voice_file_name(user_id=42, extension="ogg")
        self.assertTrue(name.startswith("42_"))
        self.assertTrue(name.endswith(".ogg"))

    async def test_cancel_user_voice_tasks(self):
        bot = TelegramBot()

        async def sleeper():
            await asyncio.sleep(60)

        task = asyncio.create_task(sleeper())
        bot._track_voice_task(99, task)

        cancelled = await bot._cancel_user_voice_tasks(99)
        self.assertEqual(cancelled, 1)
        self.assertTrue(task.cancelled())

    async def test_cleanup_stale_audio_files(self):
        with TemporaryDirectory() as td:
            audio_dir = Path(td)
            stale = audio_dir / "stale.ogg"
            fresh = audio_dir / "fresh.ogg"
            stale.write_bytes(b"OggS")
            fresh.write_bytes(b"OggS")
            stale.touch()
            fresh.touch()

            bot = TelegramBot()
            removed = await bot._cleanup_stale_audio_files(audio_dir, max_age_seconds=0)
            self.assertGreaterEqual(removed, 1)


if __name__ == "__main__":
    unittest.main()
