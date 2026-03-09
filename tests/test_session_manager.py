import importlib
import os
import sys
import unittest
from tempfile import TemporaryDirectory
from unittest.mock import patch


class SessionManagerReplyModeTests(unittest.IsolatedAsyncioTestCase):
    def _load_session_manager_module(self, project_root: str):
        with patch.dict(
            os.environ,
            {
                "PROJECT_ROOT": project_root,
                "TELEGRAM_BOT_TOKEN": "123456:abc",
            },
            clear=True,
        ):
            for name in (
                "telegram_bot.utils.config",
                "telegram_bot.session.store",
                "telegram_bot.session.manager",
            ):
                sys.modules.pop(name, None)
            return importlib.import_module("telegram_bot.session.manager")

    async def test_get_session_sets_default_reply_mode(self):
        with TemporaryDirectory() as td:
            module = self._load_session_manager_module(td)
            manager = module.SessionManager()

            session = await manager.get_session(1001)
            self.assertEqual(session["reply_mode"], "text")

    async def test_set_reply_mode_normalizes_invalid_value(self):
        with TemporaryDirectory() as td:
            module = self._load_session_manager_module(td)
            manager = module.SessionManager()

            await manager.set_reply_mode(1001, "invalid-mode")
            mode = await manager.get_reply_mode(1001)
            self.assertEqual(mode, "text")

    async def test_set_reply_mode_persists_voice(self):
        with TemporaryDirectory() as td:
            module = self._load_session_manager_module(td)
            manager = module.SessionManager()

            await manager.set_reply_mode(1001, "voice")
            mode = await manager.get_reply_mode(1001)
            self.assertEqual(mode, "voice")


if __name__ == "__main__":
    unittest.main()
