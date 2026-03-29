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
    """

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

    async def add_tool_call(self, name: str, input: dict) -> bool:
        """Add a tool call notification to the streaming output"""
        if self._finalized or not self.enable_tool_calls:
            return False

        tool_line = self._format_tool_call(name, input)
        self.tool_calls_text += tool_line

        # Rebuild full text: tool calls prefix + accumulated content
        full_text = self.tool_calls_text + self.accumulated_text

        # Create or update draft immediately (tool calls are important)
        if not self.drafts:
            await self.create_draft(full_text)
        else:
            current_draft = self.drafts[-1]
            await self.update_draft(current_draft, full_text)

        return True

    async def _retry_with_backoff(self, operation, max_retries=3):
        """Execute operation with exponential backoff on flood control errors."""
        for attempt in range(max_retries):
            try:
                return await operation()
            except RetryAfter as e:
                if attempt == max_retries - 1:
                    raise
                wait_time = (
                    float(e.retry_after) if hasattr(e, "retry_after") else (2**attempt)
                )
                logger.warning(
                    f"Rate limited, waiting {wait_time}s (retry {attempt + 1}/{max_retries})"
                )
                await asyncio.sleep(wait_time)

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

    async def update_if_needed(self, new_text_chunk: str) -> bool:
        """
        Main entry point: accumulate text and update draft if thresholds met.
        Handles overflow to new drafts when exceeding 4000 chars.
        """
        if self._finalized:
            return False

        chunk_size = len(new_text_chunk)
        logger.debug(
            f"Received chunk: {chunk_size} chars, accumulated before: {len(self.accumulated_text)} chars"
        )

        # If chunk is large, simulate progressive updates
        if chunk_size > self.min_chars:
            # Split large chunk into smaller pieces for progressive updates
            chunk_start = 0
            while chunk_start < chunk_size:
                chunk_end = min(chunk_start + self.min_chars, chunk_size)
                partial_chunk = new_text_chunk[chunk_start:chunk_end]
                self.accumulated_text += partial_chunk

                # Check for overflow
                if len(self.accumulated_text) >= 4000:
                    await self.handle_overflow()
                    chunk_start = chunk_end
                    continue

                display_text = self._first_draft_prefix() + self.accumulated_text
                # Create first draft if needed
                if not self.drafts:
                    await self.create_draft(display_text)
                else:
                    # Update existing draft
                    current_draft = self.drafts[-1]
                    await self.update_draft(current_draft, display_text)

                chunk_start = chunk_end
            return True

        # Small chunk - normal accumulation
        self.accumulated_text += new_text_chunk
        logger.debug(
            f"Accumulated {len(self.accumulated_text)} chars (chunk: {chunk_size} chars)"
        )

        # Check for overflow
        if len(self.accumulated_text) >= 4000:
            await self.handle_overflow()
            return True

        display_text = self._first_draft_prefix() + self.accumulated_text

        # Create first draft if needed
        if not self.drafts:
            await self.create_draft(display_text)
            return True

        # Update existing draft if thresholds met
        current_draft = self.drafts[-1]
        chars_since_update = len(display_text) - len(current_draft.text)
        current_draft.char_count_since_update = chars_since_update

        logger.debug(
            f"Checking update: {chars_since_update} chars since last update, min_chars={self.min_chars}"
        )

        if self.should_update(current_draft, chars_since_update):
            await self.update_draft(current_draft, display_text)
            return True

        return False

    async def finalize_all(self) -> bool:
        """Finalize all active draft messages"""
        if self._finalized:
            return False

        self._finalized = True

        # Update last draft with final accumulated text (include tool calls prefix for first draft)
        if self.drafts and self.accumulated_text:
            current_draft = self.drafts[-1]
            final_text = self._first_draft_prefix() + self.accumulated_text
            if current_draft.text != final_text:
                current_draft.text = final_text

        # Finalize all drafts
        for draft in self.drafts:
            await self.finalize_draft(draft)

        logger.debug(f"Finalized {len(self.drafts)} draft(s) for user {self.user_id}")
        return True

    async def cancel(self) -> bool:
        """Delete all unfinished draft messages and clean up state"""
        if self._finalized:
            return False

        self._finalized = True

        # Delete all draft messages
        for draft in self.drafts:
            try:
                await self._retry_with_backoff(
                    lambda: self.bot.delete_message(
                        chat_id=self.chat_id, message_id=draft.message_id
                    )
                )
                logger.debug(f"Deleted draft {draft.message_id}")
            except TelegramError as e:
                logger.error(f"Failed to delete draft {draft.message_id}: {e}")

        self.drafts.clear()
        self.accumulated_text = ""
        self.tool_calls_text = ""
        logger.debug(f"Cancelled streaming for user {self.user_id}")
        return True
