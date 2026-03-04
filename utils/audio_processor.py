import asyncio
import logging
import shutil
import time
from pathlib import Path
from typing import Iterable, Optional, Sequence

logger = logging.getLogger(__name__)


class AudioProcessor:
    """Audio format detection, conversion, and cleanup for voice messages."""

    _MP3_EXTENSIONS = {".mp3"}
    _OGG_EXTENSIONS = {".ogg", ".oga", ".opus"}
    _AMR_EXTENSIONS = {".amr"}

    def __init__(
        self,
        ffmpeg_path: Optional[str] = None,
        ffmpeg_args: Optional[Sequence[str]] = None,
    ) -> None:
        self.ffmpeg_path = (ffmpeg_path or "ffmpeg").strip() or "ffmpeg"
        self.ffmpeg_args = list(ffmpeg_args or ("-ac", "1", "-ar", "16000"))

    async def check_ffmpeg_available(self) -> bool:
        """Check ffmpeg availability from PATH or configured absolute path."""
        exists = shutil.which(self.ffmpeg_path) is not None
        if not exists:
            logger.warning("ffmpeg binary not found: %s", self.ffmpeg_path)
        return exists

    async def detect_audio_format(self, file_path: Path) -> str:
        """Detect audio format using extension first, then magic bytes."""
        suffix = file_path.suffix.lower()
        if suffix in self._MP3_EXTENSIONS:
            return "mp3"
        if suffix in self._OGG_EXTENSIONS:
            return "ogg"
        if suffix in self._AMR_EXTENSIONS:
            return "amr"

        header = b""
        try:
            with file_path.open("rb") as f:
                header = f.read(16)
        except OSError as exc:
            logger.error("Failed to read audio header from %s: %s", file_path, exc)
            return "unknown"

        if header.startswith(b"OggS"):
            return "ogg"
        if header.startswith(b"#!AMR"):
            return "amr"
        if header.startswith(b"ID3") or (len(header) >= 2 and header[0] == 0xFF):
            return "mp3"
        return "unknown"

    async def convert_audio(self, input_path: Path, output_path: Path) -> Path:
        """Convert an audio file with ffmpeg using Whisper-friendly defaults."""
        command = [
            self.ffmpeg_path,
            "-y",
            "-i",
            str(input_path),
            *self.ffmpeg_args,
            str(output_path),
        ]
        logger.debug("Running ffmpeg conversion: %s", " ".join(command))

        start = time.perf_counter()
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        elapsed_ms = int((time.perf_counter() - start) * 1000)

        if process.returncode != 0:
            stderr_text = stderr.decode("utf-8", errors="ignore").strip()
            stdout_text = stdout.decode("utf-8", errors="ignore").strip()
            detail = stderr_text or stdout_text or "unknown ffmpeg error"
            logger.error(
                "ffmpeg conversion failed (%sms), input=%s, output=%s, detail=%s",
                elapsed_ms,
                input_path,
                output_path,
                detail,
            )
            raise RuntimeError(f"ffmpeg conversion failed: {detail}")

        logger.info(
            "Audio conversion succeeded (%sms): %s -> %s",
            elapsed_ms,
            input_path,
            output_path,
        )
        return output_path

    async def cleanup_audio_files(self, file_paths: Iterable[Path]) -> None:
        """Delete temporary audio files, ignoring missing files."""
        for path in file_paths:
            try:
                if path.exists():
                    path.unlink()
                    logger.debug("Removed temporary audio file: %s", path)
            except OSError as exc:
                logger.warning(
                    "Failed to remove temporary audio file %s: %s", path, exc
                )

    async def cleanup_stale_audio_files(
        self, audio_dir: Path, max_age_seconds: int
    ) -> int:
        """Remove stale files from the audio directory."""
        if not audio_dir.exists():
            return 0

        now = time.time()
        removed = 0
        for path in audio_dir.iterdir():
            if not path.is_file():
                continue
            try:
                age = now - path.stat().st_mtime
                if age > max_age_seconds:
                    path.unlink()
                    removed += 1
            except OSError as exc:
                logger.warning("Failed to process stale audio file %s: %s", path, exc)

        if removed:
            logger.info("Removed %s stale audio file(s) from %s", removed, audio_dir)
        return removed
