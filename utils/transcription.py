import asyncio
import logging
import time
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

WHISPER_PRICE_PER_MINUTE_USD = 0.006


class TranscriptionError(RuntimeError):
    """Raised when Whisper transcription fails after retries."""


class EmptyTranscriptionError(TranscriptionError):
    """Raised when Whisper returns empty or whitespace-only text."""


class WhisperTranscriber:
    """Whisper transcription wrapper with retry and structured errors."""

    def __init__(
        self,
        api_key: Optional[str],
        model: str = "whisper-1",
        base_url: Optional[str] = None,
        client: Optional[Any] = None,
        client_factory: Optional[Callable[..., Any]] = None,
        max_retries: int = 3,
        initial_backoff: float = 1.0,
    ) -> None:
        self.api_key = (api_key or "").strip()
        self.model = model
        self.base_url = (base_url or "").strip() or None
        self.max_retries = max(1, int(max_retries))
        self.initial_backoff = max(0.1, float(initial_backoff))

        if client is not None:
            self.client = client
            return

        if not self.api_key:
            raise ValueError("OPENAI_API_KEY is required for voice transcription.")

        if client_factory is not None:
            kwargs = {"api_key": self.api_key}
            if self.base_url:
                kwargs["base_url"] = self.base_url
            self.client = client_factory(**kwargs)
            return

        try:
            import openai  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "openai package is not installed. Please add it to requirements."
            ) from exc

        if hasattr(openai, "AsyncOpenAI"):
            kwargs = {"api_key": self.api_key}
            if self.base_url:
                kwargs["base_url"] = self.base_url
            self.client = openai.AsyncOpenAI(**kwargs)
        else:
            openai.api_key = self.api_key
            if self.base_url:
                if hasattr(openai, "api_base"):
                    openai.api_base = self.base_url
                if hasattr(openai, "base_url"):
                    openai.base_url = self.base_url
            self.client = openai

    async def transcribe_audio(
        self, audio_path: Path, duration_seconds: Optional[int] = None
    ) -> str:
        """Transcribe an audio file with retries and validation."""
        start = time.perf_counter()
        for attempt in range(1, self.max_retries + 1):
            try:
                raw = await self._call_whisper(audio_path)
                text = self._extract_text(raw).strip()
                if not text:
                    raise EmptyTranscriptionError(
                        "No speech detected in the voice message."
                    )

                elapsed_ms = int((time.perf_counter() - start) * 1000)
                estimated_cost = self._estimate_cost(duration_seconds)
                logger.info(
                    "Whisper transcription succeeded (%sms), model=%s, file=%s, estimated_cost_usd=%.6f",
                    elapsed_ms,
                    self.model,
                    audio_path.name,
                    estimated_cost,
                )
                return text
            except EmptyTranscriptionError:
                raise
            except Exception as exc:
                if attempt >= self.max_retries:
                    logger.error(
                        "Whisper transcription failed after %s attempt(s): %s",
                        self.max_retries,
                        exc,
                        exc_info=True,
                    )
                    raise TranscriptionError(
                        "Unable to transcribe audio right now. Please try again."
                    ) from exc

                backoff = self.initial_backoff * (2 ** (attempt - 1))
                logger.warning(
                    "Whisper transcription attempt %s/%s failed: %s. Retrying in %.2fs.",
                    attempt,
                    self.max_retries,
                    exc,
                    backoff,
                )
                await asyncio.sleep(backoff)

        raise TranscriptionError(
            "Unable to transcribe audio right now. Please try again."
        )

    async def _call_whisper(self, audio_path: Path) -> Any:
        if hasattr(self.client, "audio") and hasattr(
            self.client.audio, "transcriptions"
        ):
            with audio_path.open("rb") as audio_file:
                return await self.client.audio.transcriptions.create(
                    model=self.model,
                    file=audio_file,
                )

        if hasattr(self.client, "Audio") and hasattr(self.client.Audio, "atranscribe"):
            with audio_path.open("rb") as audio_file:
                return await self.client.Audio.atranscribe(self.model, audio_file)

        raise TranscriptionError(
            "Unsupported OpenAI client interface for Whisper transcription."
        )

    @staticmethod
    def _extract_text(response: Any) -> str:
        if isinstance(response, str):
            return response
        if isinstance(response, dict):
            return str(response.get("text", ""))
        text = getattr(response, "text", "")
        return str(text)

    @staticmethod
    def _estimate_cost(duration_seconds: Optional[int]) -> float:
        if not duration_seconds or duration_seconds <= 0:
            return 0.0
        minutes = duration_seconds / 60
        return minutes * WHISPER_PRICE_PER_MINUTE_USD
