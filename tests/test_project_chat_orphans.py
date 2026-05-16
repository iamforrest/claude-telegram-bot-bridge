"""Tests for forwarding SDK messages that arrive after a request completes."""

# ruff: noqa: E402
import asyncio
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

_ORIGINAL_PROJECT_ROOT = os.environ.get("PROJECT_ROOT")
_ORIGINAL_CONFIG_MODULE = sys.modules.get("telegram_bot.utils.config")
os.environ["PROJECT_ROOT"] = str(Path(__file__).resolve().parents[1])

sys.modules.pop("telegram_bot.core.project_chat", None)
import telegram_bot.core.project_chat as project_chat

if _ORIGINAL_PROJECT_ROOT is None:
    os.environ.pop("PROJECT_ROOT", None)
else:
    os.environ["PROJECT_ROOT"] = _ORIGINAL_PROJECT_ROOT


class FakeTextBlock:
    def __init__(self, text):
        self.text = text


class FakeToolUseBlock:
    def __init__(self, name):
        self.name = name


class FakeAssistantMessage:
    def __init__(self, content):
        self.content = content


class FakeResultMessage:
    def __init__(
        self,
        result="",
        session_id="session-1",
        is_error=False,
        duration_ms=123,
    ):
        self.result = result
        self.session_id = session_id
        self.is_error = is_error
        self.duration_ms = duration_ms


class FakeClient:
    def __init__(self, messages):
        self.messages = list(messages)

    async def receive_messages(self):
        for msg in self.messages:
            await asyncio.sleep(0)
            yield msg


class ProjectChatOrphanTests(unittest.TestCase):
    def setUp(self):
        self._original_classes = (
            project_chat.AssistantMessage,
            project_chat.ResultMessage,
            project_chat.TextBlock,
            project_chat.ToolUseBlock,
        )
        self._original_health_reporter = project_chat.health_reporter
        project_chat.AssistantMessage = FakeAssistantMessage
        project_chat.ResultMessage = FakeResultMessage
        project_chat.TextBlock = FakeTextBlock
        project_chat.ToolUseBlock = FakeToolUseBlock
        project_chat.health_reporter = SimpleNamespace(
            record_claude_ok=lambda: None,
            record_claude_error=lambda _msg: None,
            record_sdk_pending=lambda **_kwargs: None,
        )

    def tearDown(self):
        (
            project_chat.AssistantMessage,
            project_chat.ResultMessage,
            project_chat.TextBlock,
            project_chat.ToolUseBlock,
        ) = self._original_classes
        project_chat.health_reporter = self._original_health_reporter

    def test_orphan_assistant_text_is_forwarded_to_last_chat(self):
        async def run():
            bot = SimpleNamespace(send_message=AsyncMock())
            state = project_chat._UserStreamState(
                client=FakeClient(
                    [
                        FakeAssistantMessage([FakeTextBlock("background done")]),
                        FakeResultMessage(result="background done"),
                    ]
                ),
                model=None,
                last_chat_id=456,
                last_bot=bot,
            )
            handler = project_chat.ProjectChatHandler()

            await handler._reader_loop(123, state)

            bot.send_message.assert_awaited_once_with(
                chat_id=456, text="background done"
            )
            self.assertEqual(state.orphan_assistant_texts, [])

        asyncio.run(run())

    def test_orphan_result_without_prior_text_is_forwarded(self):
        async def run():
            bot = SimpleNamespace(send_message=AsyncMock())
            state = project_chat._UserStreamState(
                client=FakeClient([FakeResultMessage(result="final report")]),
                model=None,
                last_chat_id=456,
                last_bot=bot,
            )
            handler = project_chat.ProjectChatHandler()

            await handler._reader_loop(123, state)

            bot.send_message.assert_awaited_once_with(chat_id=456, text="final report")

        asyncio.run(run())


def tearDownModule():
    sys.modules.pop("telegram_bot.core.project_chat", None)
    if _ORIGINAL_CONFIG_MODULE is None:
        sys.modules.pop("telegram_bot.utils.config", None)
        package = sys.modules.get("telegram_bot.utils")
        if package is not None and hasattr(package, "config"):
            delattr(package, "config")
    else:
        sys.modules["telegram_bot.utils.config"] = _ORIGINAL_CONFIG_MODULE


if __name__ == "__main__":
    unittest.main()
