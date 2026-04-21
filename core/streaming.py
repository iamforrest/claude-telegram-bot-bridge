"""
Streaming message handler for progressive draft message updates.
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Optional, List

from telegram import Bot
from telegram.error import TelegramError, RetryAfter

from telegram_bot.utils.config import config

logger = logging.getLogger(__name__)


@dataclass
class DraftState:
    """State for a single draft message"""

    message_id: int
    text: str
    last_update_time: float
    char_count_since_update: int = 0
    draft_id: Optional[str] = None


class StreamingMessageHandler:
    """
    Handles progressive streaming of AI responses using Telegram draft messages.

    Manages draft message lifecycle: creation, updates, finalization, and cancellation.
    Supports multi-message handling for content exceeding 4000 characters.

    Telegram API calls are serialized through a background worker task so the
    SDK reader loop (which drives update_if_needed / add_tool_call) never has to
    await Telegram flood-control backoff. The worker caps per-call backoff at
    MAX_BACKOFF_SECONDS; operations that would block longer are dropped.
    """

    # Cap on retry_after we'll honor inside the worker. A long Telegram 429
    # previously blocked the reader loop for minutes, which starved the SDK
    # stream of readers and eventually tripped the 3600s process timeout.
    MAX_BACKOFF_SECONDS = 3.0

    # Bound the caller-facing finalize latency. The worker still drains in the
    # background if it exceeds this, but process_message won't wait forever.
    FINALIZE_DRAIN_TIMEOUT = 30.0

    def __init__(self, bot: Bot, chat_id: int, user_id: int):
        self.bot = bot
        self.chat_id = chat_id
        self.user_id = user_id
        self.drafts: List[DraftState] = []
        self.accumulated_text: str = ""
        self.tool_calls_text: str = ""  # Accumulated tool call display text
        self.min_chars = config.draft_update_min_chars
        self.min_interval = config.draft_update_interval
        self.enable_tool_calls = getattr(config, "enable_streaming_tool_calls", False)
        self._finalized = False
        self._draft_seq = 0

        # Worker pipeline. Ops are zero-arg coroutine factories; a None enqueues
        # a shutdown sentinel that lets the worker exit cleanly after draining.
        self._queue: "asyncio.Queue[Optional[Any]]" = asyncio.Queue()
        self._worker_task: Optional[asyncio.Task] = None

    def _ensure_worker(self) -> None:
        """Lazily start the worker on first enqueue."""
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(self._worker_loop())

    async def _worker_loop(self) -> None:
        """Drain the op queue sequentially.

        Each op is a zero-arg coroutine factory. Per-op exceptions are logged
        and swallowed so one failing op (e.g. capped-out RetryAfter) can't
        abort subsequent ones.
        """
        while True:
            try:
                op = await self._queue.get()
            except asyncio.CancelledError:
                break
            try:
                if op is None:  # shutdown sentinel
                    break
                await op()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(
                    f"Streaming worker op failed for user {self.user_id}: "
                    f"{type(e).__name__}: {e}"
                )
            finally:
                self._queue.task_done()

    def _next_draft_id(self) -> str:
        self._draft_seq += 1
        return f"{self.user_id}-{int(time.time() * 1000)}-{self._draft_seq}"

    @staticmethod
    def _format_tool_call(name: str, input: dict) -> str:
        """Format tool call for display in Telegram"""
        # Extract key arguments for summary
        if name == "Bash" and "command" in input:
            summary = input["command"]
        elif name in ("Read", "Write", "Edit", "MultiEdit") and "file_path" in input:
            summary = input["file_path"]
        elif name == "Glob" and "pattern" in input:
            summary = input["pattern"]
        elif name == "Grep" and "pattern" in input:
            summary = input["pattern"]
        elif name == "WebFetch" and "url" in input:
            summary = input["url"]
        elif name == "WebSearch" and "query" in input:
            summary = input["query"]
        elif name == "Agent" and "subagent_type" in input:
            summary = input["subagent_type"]
        elif name == "Task" and "description" in input:
            summary = input["description"]
        elif name == "AskUserQuestion":
            # Extract question text if available
            if "questions" in input and input["questions"]:
                summary = input["questions"][0].get("question", "asking...")
            else:
                summary = "asking..."
        else:
            # Generic: show truncated input
            summary = str(input)[:80]

        return f"🛠️ **{name}**: `{summary}`\n"

    # --- Public API (sync state mutation, async I/O) --------------------

    async def update_if_needed(self, new_text_chunk: str) -> bool:
        """Accumulate a text chunk and enqueue a render. State mutation is
        synchronous so callers see the new state immediately; the Telegram
        edit_message_text call is off-loaded to the worker to keep the SDK
        reader loop from blocking on flood-control backoff.
        """
        if self._finalized:
            return False
        self.accumulated_text += new_text_chunk
        self._ensure_worker()
        self._queue.put_nowait(self._do_render_text)
        return True

    async def add_tool_call(self, name: str, input: dict) -> bool:
        """Append a tool-call line and enqueue a render with the tool prefix."""
        if self._finalized or not self.enable_tool_calls:
            return False
        self.tool_calls_text += self._format_tool_call(name, input)
        self._ensure_worker()
        self._queue.put_nowait(self._do_render_tool_call)
        return True

    async def finalize_all(self) -> bool:
        """Wait for the worker to drain, then commit final text to all drafts.

        Bounded by FINALIZE_DRAIN_TIMEOUT so a stalled Telegram flood can't
        hold up the caller indefinitely. If we time out, the worker keeps
        draining in the background until its shutdown sentinel is consumed.
        """
        if self._finalized:
            return False

        self._finalized = True
        self._ensure_worker()

        done_event = asyncio.Event()
        self._queue.put_nowait(lambda ev=done_event: self._do_finalize(ev))
        # Shutdown sentinel — processed after _do_finalize. Ensures the worker
        # exits even if the caller stops awaiting us early.
        self._queue.put_nowait(None)

        try:
            await asyncio.wait_for(
                done_event.wait(), timeout=self.FINALIZE_DRAIN_TIMEOUT
            )
        except asyncio.TimeoutError:
            logger.warning(
                f"Streaming finalize for user {self.user_id} did not drain within "
                f"{self.FINALIZE_DRAIN_TIMEOUT}s — worker continuing in background"
            )
        return True

    async def cancel(self) -> bool:
        """Stop the worker immediately, drop queued ops, delete all drafts."""
        if self._finalized:
            return False

        self._finalized = True

        # Hard-cancel the worker so queued ops don't keep touching Telegram.
        if self._worker_task and not self._worker_task.done():
            self._worker_task.cancel()
            try:
                await self._worker_task
            except (asyncio.CancelledError, Exception):
                pass

        # Delete drafts synchronously. Backoff cap keeps this bounded.
        for draft in self.drafts:
            try:
                await self._retry_with_backoff(
                    lambda d=draft: self.bot.delete_message(
                        chat_id=self.chat_id, message_id=d.message_id
                    )
                )
                logger.debug(f"Deleted draft {draft.message_id}")
            except TelegramError as e:
                logger.error(f"Failed to delete draft {draft.message_id}: {e}")
            except Exception as e:
                logger.error(f"Failed to delete draft {draft.message_id}: {e}")

        self.drafts.clear()
        self.accumulated_text = ""
        self.tool_calls_text = ""
        logger.debug(f"Cancelled streaming for user {self.user_id}")
        return True

    # --- Worker-side handlers (called from _worker_loop) ----------------

    async def _do_render_text(self) -> None:
        """Render current accumulated text to the draft, splitting on overflow.

        Reads current self.accumulated_text rather than a chunk snapshot so
        catching up after a stalled worker still converges to the correct state.
        """
        if len(self.accumulated_text) >= 4000:
            await self.handle_overflow()
            return

        display_text = self._first_draft_prefix() + self.accumulated_text

        if not self.drafts:
            await self.create_draft(display_text)
            return

        current_draft = self.drafts[-1]
        chars_since_update = len(display_text) - len(current_draft.text)
        current_draft.char_count_since_update = chars_since_update

        if self.should_update(current_draft, chars_since_update):
            await self.update_draft(current_draft, display_text)

    async def _do_render_tool_call(self) -> None:
        """Render tool_calls prefix + accumulated text immediately.

        Tool calls bypass the should_update throttle — they're rare and we
        want them visible on the draft as soon as the worker gets to them.
        Uses full tool_calls_text (not _first_draft_prefix) to preserve
        existing behavior of showing tool history even past overflow.
        """
        full_text = self.tool_calls_text + self.accumulated_text
        if not self.drafts:
            await self.create_draft(full_text)
        else:
            await self.update_draft(self.drafts[-1], full_text)

    async def _do_finalize(self, done_event: asyncio.Event) -> None:
        """Worker-side: flush final text to all drafts, then signal caller."""
        try:
            if self.drafts and self.accumulated_text:
                current_draft = self.drafts[-1]
                final_text = self._first_draft_prefix() + self.accumulated_text
                if current_draft.text != final_text:
                    current_draft.text = final_text
            for draft in self.drafts:
                await self.finalize_draft(draft)
            logger.debug(
                f"Finalized {len(self.drafts)} draft(s) for user {self.user_id}"
            )
        finally:
            done_event.set()

    # --- Telegram API helpers -------------------------------------------

    async def _retry_with_backoff(self, operation, max_retries=2):
        """Short bounded backoff on flood control.

        If Telegram asks us to wait longer than MAX_BACKOFF_SECONDS, raise
        immediately instead of sleeping. The caller logs and moves on — a
        subsequent op will retry the edit. This replaces the previous
        behavior where a single 245s RetryAfter blocked the reader loop.
        """
        for attempt in range(max_retries):
            try:
                return await operation()
            except RetryAfter as e:
                requested = float(
                    e.retry_after if hasattr(e, "retry_after") else (2 ** attempt)
                )
                if requested > self.MAX_BACKOFF_SECONDS:
                    logger.warning(
                        f"Rate limited, retry_after={requested}s exceeds cap "
                        f"{self.MAX_BACKOFF_SECONDS}s — dropping operation"
                    )
                    raise
                if attempt == max_retries - 1:
                    raise
                logger.warning(
                    f"Rate limited, waiting {requested}s "
                    f"(retry {attempt + 1}/{max_retries})"
                )
                await asyncio.sleep(requested)

    @staticmethod
    def _extract_message_id(message: Any) -> Optional[int]:
        message_id = getattr(message, "message_id", None)
        return message_id if isinstance(message_id, int) else None

    @staticmethod
    def _is_not_modified_error(error: Exception) -> bool:
        return "message is not modified" in str(error).lower()

    async def create_draft(self, text: str) -> Optional[DraftState]:
        """Send initial draft message"""
        content = text or "..."
        try:
            sent_message = await self._retry_with_backoff(
                lambda: self.bot.send_message(
                    chat_id=self.chat_id,
                    text=content,
                )
            )
            message_id = self._extract_message_id(sent_message)
            if message_id is None:
                raise RuntimeError(
                    "send_message did not return a message with valid message_id"
                )

            draft = DraftState(
                message_id=message_id,
                text=text,
                last_update_time=time.time(),
                char_count_since_update=0,
                draft_id=None,
            )
            self.drafts.append(draft)
            logger.debug(
                f"Created draft message {draft.message_id} for user {self.user_id}"
            )
            return draft
        except TelegramError as e:
            logger.error(f"Failed to create draft message: {e}")
            return None
        except Exception as e:
            logger.error(f"Failed to create draft message: {e}")
            return None

    async def update_draft(self, draft: DraftState, new_text: str) -> bool:
        """Update existing draft with new text"""
        try:
            await self._retry_with_backoff(
                lambda: self.bot.edit_message_text(
                    chat_id=self.chat_id, message_id=draft.message_id, text=new_text
                )
            )
            draft.text = new_text
            draft.last_update_time = time.time()
            draft.char_count_since_update = 0
            logger.debug(f"Updated draft {draft.message_id} ({len(new_text)} chars)")
            return True
        except TelegramError as e:
            if self._is_not_modified_error(e):
                draft.text = new_text
                draft.last_update_time = time.time()
                draft.char_count_since_update = 0
                logger.debug(
                    f"Draft {draft.message_id} unchanged on update, treated as success"
                )
                return True
            logger.error(f"Failed to update draft {draft.message_id}: {e}")
            return False

    def should_update(self, draft: DraftState, new_char_count: int) -> bool:
        """Check if draft should be updated based on thresholds"""
        time_elapsed = time.time() - draft.last_update_time
        return new_char_count >= self.min_chars or time_elapsed >= self.min_interval

    async def finalize_draft(self, draft: DraftState) -> bool:
        """Convert draft to regular message"""
        try:
            await self._retry_with_backoff(
                lambda: self.bot.edit_message_text(
                    chat_id=self.chat_id, message_id=draft.message_id, text=draft.text
                )
            )
            logger.debug(f"Finalized draft {draft.message_id}")
            return True
        except TelegramError as e:
            if self._is_not_modified_error(e):
                logger.debug(f"Draft {draft.message_id} already up-to-date on finalize")
                return True
            logger.error(f"Failed to finalize draft {draft.message_id}: {e}")
            return False

    def _find_split_boundary(self, text: str, max_length: int = 4000) -> int:
        """Find smart boundary for text splitting (paragraph > line > hard cut)"""
        if len(text) <= max_length:
            return len(text)

        # Try paragraph boundary (double newline)
        search_start = max(0, max_length - 200)
        para_idx = text.rfind("\n\n", search_start, max_length)
        if para_idx > search_start:
            return para_idx + 2

        # Try line boundary (single newline)
        line_idx = text.rfind("\n", search_start, max_length)
        if line_idx > search_start:
            return line_idx + 1

        # Hard cut at max_length
        return max_length

    def _first_draft_prefix(self) -> str:
        """Return tool calls prefix if we're still on the first draft, else empty string."""
        return self.tool_calls_text if len(self.drafts) <= 1 else ""

    async def handle_overflow(self) -> bool:
        """Handle 4000 character boundary by finalizing current draft and creating new one"""
        if not self.drafts:
            return False

        current_draft = self.drafts[-1]
        split_point = self._find_split_boundary(self.accumulated_text)

        # Finalize current draft with text up to split point
        # Include tool_calls_text prefix only on the first draft
        prefix = self._first_draft_prefix()
        finalize_text = prefix + self.accumulated_text[:split_point]
        current_draft.text = finalize_text
        await self.finalize_draft(current_draft)

        # Create new draft with remaining text
        remaining_text = self.accumulated_text[split_point:]
        self.accumulated_text = remaining_text

        if remaining_text:
            await self.create_draft(remaining_text)
            logger.debug(
                f"Created overflow draft, remaining {len(remaining_text)} chars"
            )

        return True
