import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from telegram_bot.utils.transcription import (
    EmptyTranscriptionError,
    TranscriptionError,
    WhisperTranscriber,
)


class _FakeTranscriptions:
    def __init__(self, outcomes):
        self._outcomes = list(outcomes)
        self.calls = 0

    async def create(self, **kwargs):
        del kwargs
        self.calls += 1
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class _FakeClient:
    def __init__(self, outcomes):
        self.transcriptions = _FakeTranscriptions(outcomes)
        self.audio = SimpleNamespace(transcriptions=self.transcriptions)


class WhisperTranscriberTests(unittest.IsolatedAsyncioTestCase):
    def test_passes_base_url_to_client_factory(self):
        captured = {}

        def factory(**kwargs):
            captured.update(kwargs)
            return _FakeClient([SimpleNamespace(text="ok")])

        transcriber = WhisperTranscriber(
            api_key="test-key",
            model="whisper-1",
            base_url="https://whisper-proxy.example.com/v1",
            client_factory=factory,
        )

        self.assertIsNotNone(transcriber.client)
        self.assertEqual(captured["api_key"], "test-key")
        self.assertEqual(captured["base_url"], "https://whisper-proxy.example.com/v1")

    async def test_transcribe_audio_retries_and_succeeds(self):
        with TemporaryDirectory() as td:
            audio_file = Path(td) / "voice.mp3"
            audio_file.write_bytes(b"ID3fake")

            client = _FakeClient(
                [RuntimeError("boom"), SimpleNamespace(text="hello world")]
            )
            transcriber = WhisperTranscriber(
                api_key="test-key",
                model="whisper-1",
                client=client,
                max_retries=2,
                initial_backoff=0.01,
            )

            sleep_mock = AsyncMock()
            with patch("asyncio.sleep", sleep_mock):
                text = await transcriber.transcribe_audio(
                    audio_file, duration_seconds=10
                )

            self.assertEqual(text, "hello world")
            self.assertEqual(client.transcriptions.calls, 2)
            sleep_mock.assert_awaited_once()

    async def test_transcribe_audio_rejects_empty_result(self):
        with TemporaryDirectory() as td:
            audio_file = Path(td) / "voice.mp3"
            audio_file.write_bytes(b"ID3fake")

            client = _FakeClient([SimpleNamespace(text="   ")])
            transcriber = WhisperTranscriber(
                api_key="test-key",
                model="whisper-1",
                client=client,
            )

            with self.assertRaises(EmptyTranscriptionError):
                await transcriber.transcribe_audio(audio_file)

    async def test_transcribe_audio_raises_transcription_error_after_retries(self):
        with TemporaryDirectory() as td:
            audio_file = Path(td) / "voice.mp3"
            audio_file.write_bytes(b"ID3fake")

            client = _FakeClient(
                [RuntimeError("err-1"), RuntimeError("err-2"), RuntimeError("err-3")]
            )
            transcriber = WhisperTranscriber(
                api_key="test-key",
                model="whisper-1",
                client=client,
                max_retries=3,
                initial_backoff=0.01,
            )

            with self.assertRaises(TranscriptionError) as ctx:
                await transcriber.transcribe_audio(audio_file)
            self.assertIn("Unable to transcribe audio", str(ctx.exception))

    def test_requires_api_key_when_client_not_injected(self):
        with self.assertRaises(ValueError):
            WhisperTranscriber(api_key="", client=None)


if __name__ == "__main__":
    unittest.main()
