# ruff: noqa: E402
# mypy: disable-error-code=attr-defined

import unittest
from types import SimpleNamespace
from pathlib import Path
import sys
import types
from telegram.error import TelegramError

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

config_module = types.ModuleType("telegram_bot.utils.config")
config_module.config = SimpleNamespace(
    draft_update_min_chars=20,
    draft_update_interval=0.1,
)
sys.modules["telegram_bot.utils.config"] = config_module

from telegram_bot.core.streaming import StreamingMessageHandler


class _BotWithDraftId:
    def __init__(self):
        self.calls = []

    async def send_message(self, *, chat_id, text):
        self.calls.append(("send_message", chat_id, text))
        return SimpleNamespace(message_id=101)


class _BotDraftSignatureMismatch:
    def __init__(self):
        self.calls = []

    # Intentionally no draft_id parameter
    async def send_message_draft(self, *, chat_id, text):
        self.calls.append(("send_message_draft", chat_id, text))
        return SimpleNamespace(message_id=999)

    async def send_message(self, *, chat_id, text):
        self.calls.append(("send_message", chat_id, text))
        return SimpleNamespace(message_id=202)


class _BotDraftReturnsBool:
    def __init__(self):
        self.calls = []

    async def send_message_draft(self, *, chat_id, draft_id, text):
        self.calls.append(("send_message_draft", chat_id, draft_id, text))
        return True

    async def send_message(self, *, chat_id, text):
        self.calls.append(("send_message", chat_id, text))
        return SimpleNamespace(message_id=303)


class _BotEditNotModified:
    async def edit_message_text(self, *, chat_id, message_id, text):
        raise TelegramError(
            "Message is not modified: specified new message content and reply markup are exactly the same as a current content and reply markup of the message"
        )


class StreamingMessageHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_draft_uses_draft_api_with_draft_id(self):
        bot = _BotWithDraftId()
        handler = StreamingMessageHandler(bot=bot, chat_id=42, user_id=7)

        draft = await handler.create_draft("hello")

        self.assertIsNotNone(draft)
        self.assertEqual(draft.message_id, 101)
        self.assertIsNone(draft.draft_id)
        self.assertEqual(len(bot.calls), 1)
        self.assertEqual(bot.calls[0][0], "send_message")

    async def test_create_draft_falls_back_to_send_message_on_signature_mismatch(self):
        bot = _BotDraftSignatureMismatch()
        handler = StreamingMessageHandler(bot=bot, chat_id=42, user_id=7)

        draft = await handler.create_draft("hello")

        self.assertIsNotNone(draft)
        self.assertEqual(draft.message_id, 202)
        self.assertIsNone(draft.draft_id)
        self.assertEqual(len(bot.calls), 1)
        self.assertEqual(bot.calls[0][0], "send_message")

    async def test_create_draft_uses_send_message_even_if_draft_api_exists(self):
        bot = _BotDraftReturnsBool()
        handler = StreamingMessageHandler(bot=bot, chat_id=42, user_id=7)

        draft = await handler.create_draft("hello")

        self.assertIsNotNone(draft)
        self.assertEqual(draft.message_id, 303)
        self.assertIsNone(draft.draft_id)
        self.assertEqual(len(bot.calls), 1)
        self.assertEqual(bot.calls[0][0], "send_message")

    async def test_update_draft_treats_not_modified_as_success(self):
        bot = _BotEditNotModified()
        handler = StreamingMessageHandler(bot=bot, chat_id=42, user_id=7)
        draft = SimpleNamespace(
            message_id=992,
            text="old",
            last_update_time=0.0,
            char_count_since_update=10,
        )

        ok = await handler.update_draft(draft, "same")

        self.assertTrue(ok)
        self.assertEqual(draft.text, "same")
        self.assertEqual(draft.char_count_since_update, 0)

    async def test_finalize_draft_treats_not_modified_as_success(self):
        bot = _BotEditNotModified()
        handler = StreamingMessageHandler(bot=bot, chat_id=42, user_id=7)
        draft = SimpleNamespace(message_id=992, text="same")

        ok = await handler.finalize_draft(draft)

        self.assertTrue(ok)


if __name__ == "__main__":
    unittest.main()
