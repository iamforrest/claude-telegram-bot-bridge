import asyncio
import logging
import re
import shutil
import uuid
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)


class TtsSynthesisError(RuntimeError):
    """Raised when macOS TTS synthesis or conversion fails."""


class VoicePersonaNotAvailableError(TtsSynthesisError):
    """Raised when configured voice persona is unavailable on current macOS."""

    def __init__(self, persona: str, available_voices: List[str]):
        self.persona = persona
        self.available_voices = available_voices
        super().__init__(
            f"Configured VOICE_REPLY_PERSONA is unavailable. persona={persona}"
        )


class MacOSTtsSynthesizer:
    def __init__(
        self,
        *,
        ffmpeg_path: Optional[str] = None,
        say_path: str = "say",
    ):
        self.say_path = say_path
        self.ffmpeg_path = ffmpeg_path or "ffmpeg"
        self._available_voices_cache: Optional[List[str]] = None

    async def list_available_voices(self) -> List[str]:
        if self._available_voices_cache is not None:
            return list(self._available_voices_cache)

        process = await asyncio.create_subprocess_exec(
            self.say_path,
            "-v",
            "?",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            raise TtsSynthesisError(
                f"Failed to list macOS voices: {stderr.decode('utf-8', errors='ignore').strip()}"
            )

        voices: List[str] = []
        for raw_line in stdout.decode("utf-8", errors="ignore").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            match = re.match(r"^(.+?)\s{2,}\S+\s+#", line)
            voice = (match.group(1) if match else line.split(maxsplit=1)[0]).strip()
            if voice:
                voices.append(voice)

        if not voices:
            raise TtsSynthesisError("No macOS voices were returned by `say -v ?`.")

        self._available_voices_cache = voices
        return list(voices)

    async def resolve_voice(self, persona: Optional[str]) -> str:
        available_voices = await self.list_available_voices()
        available_set = set(available_voices)
        requested_voice = str(persona or "Tingting").strip() or "Tingting"
        if requested_voice and requested_voice in available_set:
            return requested_voice
        raise VoicePersonaNotAvailableError(
            persona=requested_voice,
            available_voices=available_voices,
        )

    async def synthesize_to_telegram_voice(
        self,
        *,
        text: str,
        output_dir: Path,
        persona: Optional[str] = None,
    ) -> Tuple[Path, List[Path], str]:
        plain_text = str(text or "").strip()
        if not plain_text:
            raise TtsSynthesisError("Cannot synthesize empty text.")
        if shutil.which(self.ffmpeg_path) is None:
            raise TtsSynthesisError("ffmpeg is not installed or not found in PATH.")

        output_dir.mkdir(parents=True, exist_ok=True)
        file_id = uuid.uuid4().hex
        aiff_path = output_dir / f"reply_{file_id}.aiff"
        ogg_path = output_dir / f"reply_{file_id}.ogg"
        cleanup_paths = [aiff_path, ogg_path]

        voice_name = await self.resolve_voice(persona)
        await self._run_say(
            text=plain_text, voice_name=voice_name, output_path=aiff_path
        )
        await self._convert_to_ogg_opus(source_path=aiff_path, output_path=ogg_path)
        return ogg_path, cleanup_paths, voice_name

    async def _run_say(self, *, text: str, voice_name: str, output_path: Path) -> None:
        process = await asyncio.create_subprocess_exec(
            self.say_path,
            "-v",
            voice_name,
            "-o",
            str(output_path),
            text,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate()
        if process.returncode != 0:
            raise TtsSynthesisError(
                f"macOS say synthesis failed: {stderr.decode('utf-8', errors='ignore').strip()}"
            )

    async def _convert_to_ogg_opus(
        self, *, source_path: Path, output_path: Path
    ) -> None:
        process = await asyncio.create_subprocess_exec(
            self.ffmpeg_path,
            "-y",
            "-i",
            str(source_path),
            "-vn",
            "-c:a",
            "libopus",
            "-b:a",
            "32k",
            "-ac",
            "1",
            str(output_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate()
        if process.returncode != 0:
            raise TtsSynthesisError(
                f"ffmpeg voice conversion failed: {stderr.decode('utf-8', errors='ignore').strip()}"
            )
