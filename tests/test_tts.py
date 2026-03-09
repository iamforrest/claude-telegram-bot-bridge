import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from telegram_bot.utils.tts import (
    MacOSTtsSynthesizer,
    TtsSynthesisError,
    VoicePersonaNotAvailableError,
)


class _FakeProcess:
    def __init__(self, returncode=0, stdout=b"", stderr=b""):
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr

    async def communicate(self):
        return self._stdout, self._stderr


class MacOSTtsSynthesizerTests(unittest.IsolatedAsyncioTestCase):
    async def test_list_available_voices_parses_premium_voice_name(self):
        synth = MacOSTtsSynthesizer()
        process = _FakeProcess(
            returncode=0,
            stdout=(
                b"Yue (Premium)             zh_CN    # \xe4\xbd\xa0\xe5\xa5\xbd\n"
                b"Tingting                  zh_CN    # \xe4\xbd\xa0\xe5\xa5\xbd\n"
            ),
        )
        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=process)):
            voices = await synth.list_available_voices()

        self.assertIn("Yue (Premium)", voices)
        self.assertIn("Tingting", voices)

    async def test_resolve_voice_uses_system_voice_name_when_available(self):
        synth = MacOSTtsSynthesizer()
        synth.list_available_voices = AsyncMock(
            return_value=["Tingting", "Yue (Premium)", "Samantha"]
        )

        resolved = await synth.resolve_voice("Yue (Premium)")
        self.assertEqual(resolved, "Yue (Premium)")

    async def test_resolve_voice_raises_when_requested_voice_is_missing(self):
        synth = MacOSTtsSynthesizer()
        synth.list_available_voices = AsyncMock(return_value=["Alex", "Tingting"])

        with self.assertRaises(VoicePersonaNotAvailableError):
            await synth.resolve_voice("MissingVoice")

    async def test_synthesize_to_telegram_voice_raises_when_say_fails(self):
        synth = MacOSTtsSynthesizer()
        synth.resolve_voice = AsyncMock(return_value="Tingting")

        with TemporaryDirectory() as td:
            with (
                patch("shutil.which", return_value="/usr/bin/ffmpeg"),
                patch(
                    "asyncio.create_subprocess_exec",
                    AsyncMock(
                        return_value=_FakeProcess(returncode=1, stderr=b"say failed")
                    ),
                ),
            ):
                with self.assertRaises(TtsSynthesisError) as ctx:
                    await synth.synthesize_to_telegram_voice(
                        text="hello",
                        output_dir=Path(td),
                    )
        self.assertIn("macOS say synthesis failed", str(ctx.exception))

    async def test_synthesize_to_telegram_voice_raises_when_ffmpeg_fails(self):
        synth = MacOSTtsSynthesizer()
        synth.resolve_voice = AsyncMock(return_value="Tingting")
        create_subprocess_exec = AsyncMock(
            side_effect=[
                _FakeProcess(returncode=0),
                _FakeProcess(returncode=1, stderr=b"ffmpeg failed"),
            ]
        )

        with TemporaryDirectory() as td:
            with (
                patch("shutil.which", return_value="/usr/bin/ffmpeg"),
                patch("asyncio.create_subprocess_exec", create_subprocess_exec),
            ):
                with self.assertRaises(TtsSynthesisError) as ctx:
                    await synth.synthesize_to_telegram_voice(
                        text="hello",
                        output_dir=Path(td),
                    )
        self.assertIn("ffmpeg voice conversion failed", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
