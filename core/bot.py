import asyncio
import json
import logging
import re
import shlex
import time
from pathlib import Path as FilePath
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional, Tuple
from datetime import datetime, timezone

import telegram.error
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllChatAdministrators,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from telegram_bot.utils.config import config
from telegram_bot.session.manager import session_manager
from telegram_bot.core.project_chat import project_chat_handler, ChatResponse
from claude_code_sdk.types import PermissionResultAllow, PermissionResultDeny
from telegram_bot.utils.chat_logger import log_debug
from telegram_bot.utils.audio_processor import AudioProcessor
from telegram_bot.utils.transcription import (
    EmptyTranscriptionError,
    TranscriptionError,
    WhisperTranscriber,
)

logger = logging.getLogger(__name__)
STALE_MESSAGE_SECONDS = 20 * 60  # 20 minutes


def _esc_md2(text: str) -> str:
    """Escape MarkdownV2 special characters."""
    return re.sub(r"([_*\[\]()~`>#+=|{}.!\\-])", r"\\\1", text)


class TelegramBot:
    def __init__(self):
        self.application: Optional[Application] = None
        # Only sessions created/resumed in current runtime are auto-resumed.
        self._runtime_active_sessions: set[int] = set()
        self._runtime_active_voice_sessions: set[int] = set()
        self._user_run_tasks: Dict[int, set[asyncio.Task]] = {}
        self._user_voice_tasks: Dict[int, set[asyncio.Task]] = {}
        self._user_queue_locks: Dict[int, asyncio.Lock] = {}
        # Track currently executing task per user for priority stop command
        self._active_tasks: Dict[int, asyncio.Task] = {}
        self._audio_dir = config.bot_data_dir / "audio"
        self._audio_processor = AudioProcessor(ffmpeg_path=config.ffmpeg_path)
        self._whisper_transcriber: Optional[WhisperTranscriber] = None

    # Available models for /model command
    MODELS = [
        ("sonnet", "Claude Sonnet"),
        ("opus", "Claude Opus"),
        ("haiku", "Claude Haiku"),
    ]
    _PATH_GUARDED_TOOLS = {"Read", "Edit", "Write", "MultiEdit", "Glob", "Grep", "Bash"}
    _ALLOW_OUTSIDE_ONCE_TOKEN = "ALLOW_OUTSIDE_ONCE"
    _DENY_OUTSIDE_TOKEN = "DENY_OUTSIDE"
    _PATH_KEYWORDS = ("path", "file", "cwd", "dir", "directory", "root")
    _MAX_INFLIGHT_MESSAGES = 3
    _STALE_AUDIO_SECONDS = 24 * 60 * 60

    async def _post_init(self, application: Application):
        """Called after application.initialize() by run_polling()"""
        self._audio_dir.mkdir(parents=True, exist_ok=True)
        removed = await self._cleanup_stale_audio_files(
            self._audio_dir, max_age_seconds=self._STALE_AUDIO_SECONDS
        )
        if removed:
            logger.info("Startup audio cleanup removed %s stale file(s)", removed)
        await self._set_bot_commands()
        logger.info("✅ Bot initialization complete")

    def build(self):
        """Build the application"""
        self.application = (
            Application.builder()
            .token(config.telegram_bot_token)
            .concurrent_updates(True)
            .post_init(self._post_init)
            .build()
        )
        self._setup_handlers()
        self.application.add_error_handler(self._error_handler)

    def run(self):
        """Run the bot with built-in signal handling and graceful shutdown"""
        if not self.application:
            self.build()

        logger.info("⏳ Starting...")
        try:
            self.application.run_polling()
        except telegram.error.InvalidToken:
            raise SystemExit(
                "❌ Invalid Telegram Bot Token. "
                "Please check TELEGRAM_BOT_TOKEN in your .env file.\n"
                "   Get a valid token from @BotFather on Telegram."
            )
        except telegram.error.Conflict:
            raise SystemExit(
                "❌ Another bot instance is already running with the same token.\n"
                "   Use --stop to stop it first, or check for duplicate processes."
            )
        except telegram.error.NetworkError as e:
            raise SystemExit(
                f"❌ Network error: {e}\n"
                "   Check your internet connection and PROXY_URL settings."
            )
        except telegram.error.Forbidden as e:
            raise SystemExit(
                f"❌ Bot token was revoked or bot is blocked: {e}\n"
                "   Create a new token via @BotFather on Telegram."
            )

        logger.info("Bot stopped")

    def _check_user_access(self, user_id: int) -> bool:
        """Check if user has permission to use the bot"""
        if not config.allowed_user_ids:
            return True  # Allow all users if not configured
        return user_id in config.allowed_user_ids

    async def _check_access(self, update: Update) -> bool:
        """Check if user has permission to use this bot

        Returns:
            bool: True if user has permission, False otherwise
        """
        # Drop stale messages (> 20 min old)
        msg = update.message or update.callback_query and update.callback_query.message
        if msg and msg.date:
            age = (datetime.now(timezone.utc) - msg.date).total_seconds()
            if age > STALE_MESSAGE_SECONDS:
                logger.debug(
                    f"Dropping stale message ({age:.0f}s old) from {update.effective_user}"
                )
                return False

        user = update.effective_user
        if not user:
            return False

        # Check if user is in the allowed list
        if not self._check_user_access(user.id):
            # Send different rejection messages based on update type
            if update.message:
                if update.message.voice:
                    await update.message.reply_text(
                        "⛔ You don't have permission to send voice messages to this bot.\n"
                        "Please contact the admin for access."
                    )
                else:
                    await update.message.reply_text(
                        "⛔ Sorry, you don't have permission to use this bot.\n"
                        "Please contact the admin for access."
                    )
            elif update.callback_query:
                await update.callback_query.answer(
                    "⛔ No permission to use this feature", show_alert=True
                )
            return False
        return True

    @staticmethod
    def _is_priority_command(text: str) -> bool:
        """Check if a command should be processed with priority (bypass queue).

        Priority commands are processed immediately without queue limit checks.
        Currently only /stop is a priority command.
        """
        return text.strip() == "/stop"

    @staticmethod
    def _is_within_project_root(path: FilePath) -> bool:
        from telegram_bot.core.project_chat import PROJECT_ROOT

        try:
            return path.resolve(strict=False).is_relative_to(PROJECT_ROOT)
        except Exception:
            return False

    @staticmethod
    def _resolve_candidate_path(raw_path: str) -> FilePath:
        from telegram_bot.core.project_chat import PROJECT_ROOT

        candidate = FilePath(raw_path.strip().strip("\"'")).expanduser()
        if not candidate.is_absolute():
            candidate = PROJECT_ROOT / candidate
        return candidate.resolve(strict=False)

    @staticmethod
    def _iter_strings(value: Any) -> Iterable[str]:
        if isinstance(value, str):
            yield value
            return
        if isinstance(value, dict):
            for item in value.values():
                yield from TelegramBot._iter_strings(item)
            return
        if isinstance(value, (list, tuple)):
            for item in value:
                yield from TelegramBot._iter_strings(item)

    @staticmethod
    def _extract_paths_from_command(command: str) -> List[str]:
        try:
            tokens = shlex.split(command)
        except Exception:
            tokens = command.split()

        candidates: List[str] = []
        for token in tokens:
            token = token.strip()
            if not token or token.startswith("-") or "://" in token:
                continue
            if token.startswith(("~", "/", "./", "../")) or "/" in token:
                candidates.append(token)
        return candidates

    def _extract_path_candidates(self, tool_name: str, tool_input: Any) -> List[str]:
        candidates: List[str] = []
        seen = set()

        def add_candidate(raw: str):
            raw = raw.strip()
            if not raw or raw in seen:
                return
            seen.add(raw)
            candidates.append(raw)

        def walk(value: Any, parent_key: str = ""):
            if isinstance(value, dict):
                for key, item in value.items():
                    key_lower = key.lower()
                    if isinstance(item, str) and any(
                        word in key_lower for word in self._PATH_KEYWORDS
                    ):
                        add_candidate(item)
                    else:
                        walk(item, key_lower)
                return
            if isinstance(value, (list, tuple)):
                for item in value:
                    walk(item, parent_key)
                return
            if isinstance(value, str) and parent_key == "command":
                for token in self._extract_paths_from_command(value):
                    add_candidate(token)

        walk(tool_input)
        if tool_name == "Bash":
            for text in self._iter_strings(tool_input):
                for token in self._extract_paths_from_command(text):
                    add_candidate(token)
        return candidates

    def _extract_outside_paths(self, tool_name: str, tool_input: Any) -> List[str]:
        if tool_name not in self._PATH_GUARDED_TOOLS:
            return []
        outside: List[str] = []
        seen = set()
        for raw_path in self._extract_path_candidates(tool_name, tool_input):
            try:
                resolved = self._resolve_candidate_path(raw_path)
            except Exception:
                continue
            if not self._is_within_project_root(resolved):
                path_str = str(resolved)
                if path_str not in seen:
                    seen.add(path_str)
                    outside.append(path_str)
        return outside

    async def _consume_outside_approval_once(self, user_id: int) -> bool:
        session = await session_manager.get_session(user_id)
        if not session.get("outside_path_approved_once"):
            return False
        session["outside_path_approved_once"] = False
        session.pop("pending_outside_paths", None)
        await session_manager.update_session(user_id, session)
        return True

    async def _maybe_capture_outside_approval(self, user_id: int, text: str) -> None:
        session = await session_manager.get_session(user_id)
        pending = session.get("pending_outside_paths")
        if not pending:
            return

        normalized = text.strip().lower()
        allow = self._ALLOW_OUTSIDE_ONCE_TOKEN.lower() in normalized or normalized in {
            "1",
            "allow",
            "yes",
            "y",
        }
        deny = self._DENY_OUTSIDE_TOKEN.lower() in normalized or normalized in {
            "2",
            "deny",
            "no",
            "n",
        }

        if allow:
            session["outside_path_approved_once"] = True
            session.pop("pending_outside_paths", None)
            await session_manager.update_session(user_id, session)
        elif deny:
            session["outside_path_approved_once"] = False
            session.pop("pending_outside_paths", None)
            await session_manager.update_session(user_id, session)

    async def _permission_callback(self, chat_id: int, user_id: int, tool_name: str, tool_input: Any):
        """Handle tool permission requests.

        All interactive requests are denied so Claude falls back to numbered
        text options, which the bot detects and presents as inline keyboard buttons.
        """
        if tool_name == "AskUserQuestion":
            return PermissionResultDeny(
                message=(
                    "AskUserQuestion is not available in this environment. "
                    "IMPORTANT: Do NOT mention this to the user. Do NOT say the tool is unavailable or that user didn't select. "
                    "Instead, you MUST output the question and options in this EXACT format:\n\n"
                    "[Question text here]\n\n"
                    "1. [First option]\n"
                    "2. [Second option]\n"
                    "3. [Third option]\n\n"
                    "Example:\n"
                    "确认发布内容：\n"
                    "[show the content here with absolute file paths like /Users/.../image.png]\n\n"
                    "1. 去发布\n"
                    "2. 重新生成\n"
                    "3. 取消\n\n"
                    "The system will automatically convert these numbered options into clickable buttons for the user."
                )
            )

        outside_paths = self._extract_outside_paths(tool_name, tool_input)
        if outside_paths:
            if await self._consume_outside_approval_once(user_id):
                return PermissionResultAllow()

            session = await session_manager.get_session(user_id)
            session["pending_outside_paths"] = outside_paths[:5]
            await session_manager.update_session(user_id, session)

            preview = "\n".join(f"- {path}" for path in outside_paths[:5])
            return PermissionResultDeny(
                message=(
                    "Detected access to paths outside PROJECT_ROOT. Requires confirmation before proceeding.\n"
                    f"{preview}\n"
                    "Please output the following two options to the user and wait for a reply:\n"
                    f"1. {self._ALLOW_OUTSIDE_ONCE_TOKEN} (Allow this external path access)\n"
                    f"2. {self._DENY_OUTSIDE_TOKEN} (Deny)"
                )
            )

        return PermissionResultAllow()

    async def _save_session_id(self, user_id: int, response: ChatResponse):
        if response.session_id:
            session = await session_manager.get_session(user_id)
            session["session_id"] = response.session_id
            await session_manager.update_session(user_id, session)
            self._runtime_active_sessions.add(user_id)

    def _effective_session_id(self, user_id: int, session: dict) -> Optional[str]:
        """Prevent cross-process auto-resume from persisted session data."""
        session_id = session.get("session_id")
        if not session_id:
            return None
        if user_id not in self._runtime_active_sessions:
            logger.info(
                f"Ignoring persisted session_id for user {user_id} (not active in current runtime)"
            )
            return None
        return session_id

    def _setup_handlers(self):
        # Command handlers
        self.application.add_handler(CommandHandler("start", self._cmd_start))
        self.application.add_handler(CommandHandler("skills", self._cmd_skills))
        self.application.add_handler(CommandHandler("new", self._cmd_new))
        self.application.add_handler(CommandHandler("model", self._cmd_model))
        self.application.add_handler(CommandHandler("resume", self._cmd_resume))
        self.application.add_handler(CommandHandler("stop", self._cmd_stop))
        self.application.add_handler(CommandHandler("history", self._cmd_history))
        self.application.add_handler(CommandHandler("command", self._cmd_command))
        self.application.add_handler(CommandHandler("skill", self._cmd_skill))

        # Skill command handler - catches all /commands
        self.application.add_handler(
            MessageHandler(filters.COMMAND, self._handle_skill_command), group=1
        )

        # Text message handler - for answers to questions
        self.application.add_handler(
            MessageHandler(filters.VOICE, self._handle_voice_message), group=2
        )
        self.application.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_text_message),
            group=2,
        )

        # Callback query handler - for inline keyboards
        self.application.add_handler(CallbackQueryHandler(self._handle_callback))

    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_access(update):
            return

        user = update.effective_user
        log_debug(user.id, "command", "/start")
        welcome_text = f"👋 Hello, {user.first_name}! Send a message to start chatting, or use /skills to view available skills."
        await update.message.reply_text(welcome_text)
        log_debug(user.id, "bot", welcome_text)

    async def _cmd_skills(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_access(update):
            return

        user_id = update.effective_user.id
        log_debug(user_id, "command", "/skills")

        await update.message.chat.send_action(action="typing")

        prompt = (
            "List all installed skills, grouped by global and project.\n"
            "Output format requirements (strictly follow):\n"
            "- Use Telegram HTML format, group titles in <b>Title</b> bold\n"
            "- One skill per line, format: /skill_name description\n"
            "- Do NOT use Markdown syntax (no ## or **)\n"
            "- Do NOT output any extra introductory text or status lines"
        )
        response = await project_chat_handler.process_message(
            user_message=prompt,
            user_id=user_id,
            chat_id=update.effective_chat.id,
            new_session=True,
            permission_callback=self._permission_callback,
            typing_callback=lambda: update.message.chat.send_action(action="typing"),
        )
        await self._save_session_id(user_id, response)
        await update.message.reply_text(response.content, parse_mode="HTML")
        log_debug(user_id, "bot", response.content)

    async def _cmd_new(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_access(update):
            return
        user_id = update.effective_user.id
        log_debug(user_id, "command", "/new")

        cancelled_voice = await self._cancel_user_voice_tasks(user_id)
        if cancelled_voice:
            logger.info(
                "Cancelled %s active voice task(s) for user %s on /new",
                cancelled_voice,
                user_id,
            )

        # Cancel any ongoing streaming
        await self._cancel_user_streaming(user_id)

        session = await session_manager.get_session(user_id)
        session["session_id"] = None
        session["new_session"] = True

        # Sync session model with settings.json; clear if settings changed
        try:
            with open(config.claude_settings_path, "r") as f:
                settings_model = json.load(f).get("model")
        except Exception:
            settings_model = None

        if session.get("model") != settings_model:
            old_model = session.get("model")
            session["model"] = settings_model
            effective = self._get_real_model(session)
            logger.info(
                f"User {user_id}: model synced {old_model!r} -> {settings_model!r} (effective: {effective!r}) on /new"
            )
            log_debug(
                user_id,
                "model",
                f"Auto-synced model: {old_model} -> {settings_model} (effective: {effective})",
            )

        await session_manager.update_session(user_id, session)
        self._runtime_active_sessions.discard(user_id)
        reply = "🆕 Switched to new session mode. Your next message will start a new Claude Code session."
        await update.message.reply_text(reply)
        log_debug(user_id, "bot", reply)

    def _get_real_model(self, session: dict) -> str:
        """Get current model from session or ~/.claude/settings.json"""
        if model := session.get("model"):
            return model
        try:
            with open(config.claude_settings_path, "r") as f:
                return json.load(f).get("model", "sonnet")
        except Exception:
            return "sonnet"

    async def _cmd_model(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_access(update):
            return
        user_id = update.effective_user.id
        log_debug(user_id, "command", "/model")
        session = await session_manager.get_session(user_id)

        if context.args:
            name = context.args[0]
            session["model"] = name
            await session_manager.update_session(user_id, session)
            label = dict(self.MODELS).get(name, name)
            logger.info(f"User {user_id}: model set to {name!r} via /model command")
            reply = f"✅ Switched to {label}"
            await update.message.reply_text(reply)
            log_debug(user_id, "bot", reply)
            return

        current_model = self._get_real_model(session)
        models = list(self.MODELS)
        if current_model not in dict(models):
            models.append((current_model, current_model))
        buttons = [
            [
                InlineKeyboardButton(
                    f"{label} (current)" if name == current_model else label,
                    callback_data=f"model:{name}",
                )
            ]
            for name, label in models
        ]
        reply = "🤖 Select Claude Code model:"
        await update.message.reply_text(
            reply, reply_markup=InlineKeyboardMarkup(buttons)
        )
        log_debug(user_id, "bot", reply)

    async def _cmd_resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_access(update):
            return
        user_id = update.effective_user.id
        log_debug(user_id, "command", "/resume")
        sessions = project_chat_handler.list_sessions(limit=10)
        if not sessions:
            reply = "📭 No session history found."
            await update.message.reply_text(reply)
            log_debug(user_id, "bot", reply)
            return

        # Store session list for later selection
        session = await session_manager.get_session(user_id)
        session["resume_list"] = [(sid, msg) for sid, msg, _ in sessions]
        await session_manager.update_session(user_id, session)

        def _esc_resume_text(text: str) -> str:
            text = re.sub(r"https?://\S+", "", text).strip()
            return _esc_md2(text)

        def relative_time(mtime: float) -> str:
            delta = int(time.time() - mtime)
            if delta < 60:
                return f"{delta} seconds ago"
            if delta < 3600:
                return f"{delta // 60} minutes ago"
            if delta < 86400:
                return f"{delta // 3600} hours ago"
            return datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")

        NUM_EMOJI = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]

        lines = ["📋 *Session History*\n"]
        for i, (sid, msg, mtime) in enumerate(sessions, 1):
            ts = relative_time(mtime)
            esc = _esc_resume_text(msg.replace("\n", " "))
            if i > 1:
                lines.append("")
            num = NUM_EMOJI[i - 1] if i <= len(NUM_EMOJI) else f"*{i}\\.*"
            lines.append(f"{num} {esc}")
            lines.append(_esc_resume_text(ts))
        lines.append(f"\n{_esc_md2('Reply with a number to switch to that session:')}")
        reply = "\n".join(lines)
        await update.message.reply_text(reply, parse_mode="MarkdownV2")
        log_debug(user_id, "bot", reply)

    async def _cmd_stop(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /stop - interrupt active execution and clear queue.

        This command has priority handling - it bypasses queue limits and
        immediately cancels any running task for the user.
        """
        if not await self._check_access(update):
            return
        user_id = update.effective_user.id
        log_debug(user_id, "command", "/stop")

        cancelled_voice = await self._cancel_user_voice_tasks(user_id)
        if cancelled_voice:
            logger.info(
                "Cancelled %s active voice task(s) for user %s on /stop",
                cancelled_voice,
                user_id,
            )

        # Cancel any ongoing streaming
        await self._cancel_user_streaming(user_id)

        # Cancel the currently executing task (priority stop)
        active_task = self._active_tasks.get(user_id)
        task_cancelled = False
        if active_task and not active_task.done():
            active_task.cancel()
            task_cancelled = True
            logger.info(
                "Cancelled active task for user %s via priority /stop command",
                user_id,
            )

        killed = await project_chat_handler.stop(user_id)
        cleared = self._clear_user_queue(user_id)

        # Build response message - simple and friendly
        if task_cancelled or killed or cleared:
            reply = "⏸️ Paused"
        else:
            reply = "ℹ️ Nothing running"
        await update.message.reply_text(reply)
        log_debug(user_id, "bot", reply)

    async def _cmd_history(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /history - display recent messages from current session."""
        if not await self._check_access(update):
            return
        user_id = update.effective_user.id
        log_debug(user_id, "command", "/history")

        session = await session_manager.get_session(user_id)
        session_id = session.get("session_id")

        if not session_id:
            reply = "📭 No active session. Start a conversation first."
            await update.message.reply_text(reply)
            log_debug(user_id, "bot", reply)
            return

        messages = project_chat_handler.get_recent_messages(session_id, limit=5)

        if not messages:
            reply = "📭 No history available for this session."
            await update.message.reply_text(reply)
            log_debug(user_id, "bot", reply)
            return

        lines = ["📜 Recent History (last 5 messages)\n"]
        for msg in messages:
            role = msg["role"]
            content = msg["content"]
            timestamp = msg["timestamp"]

            # Format emoji indicator
            emoji = "🧑" if role == "user" else "🤖"
            role_label = "User" if role == "user" else "Assistant"

            # Format timestamp
            try:
                from datetime import datetime
                dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                ts_str = dt.strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                ts_str = timestamp[:19] if len(timestamp) >= 19 else timestamp

            # Truncate content
            if len(content) > 500:
                content = content[:500] + "..."

            lines.append(f"{emoji} {role_label} [{ts_str}]")
            lines.append(content)
            lines.append("")

        reply = "\n".join(lines).strip()

        # Ensure total length under 4000 chars
        if len(reply) > 4000:
            reply = reply[:3997] + "..."

        await update.message.reply_text(reply)
        log_debug(user_id, "bot", reply)

    async def _error_handler(self, update: object, context: ContextTypes.DEFAULT_TYPE):
        """Global error handler for uncaught exceptions in handlers."""
        logger.error("Unhandled exception:", exc_info=context.error)
        if isinstance(update, Update) and update.effective_chat:
            try:
                await context.bot.send_message(
                    update.effective_chat.id, f"❌ Internal error: {context.error}"
                )
            except Exception:
                pass

    async def _cancel_user_streaming(self, user_id: int) -> bool:
        """Cancel streaming for a user"""
        try:
            return await project_chat_handler.cancel_user_streaming(user_id)
        except Exception as e:
            logger.error(f"Failed to cancel streaming for user {user_id}: {e}")
            return False

    def _prune_voice_tasks(self, user_id: int) -> set[asyncio.Task]:
        tasks = self._user_voice_tasks.get(user_id)
        if not tasks:
            tasks = set()
            self._user_voice_tasks[user_id] = tasks
            return tasks
        done = {t for t in tasks if t.done()}
        tasks.difference_update(done)
        return tasks

    def _track_voice_task(self, user_id: int, task: asyncio.Task) -> None:
        tasks = self._prune_voice_tasks(user_id)
        tasks.add(task)
        self._runtime_active_voice_sessions.add(user_id)

        def _on_done(t: asyncio.Task) -> None:
            current = self._user_voice_tasks.get(user_id)
            if current is not None:
                current.discard(t)
                if not current:
                    self._runtime_active_voice_sessions.discard(user_id)
            try:
                t.result()
            except asyncio.CancelledError:
                logger.debug("Voice task cancelled for user %s", user_id)
            except Exception as exc:
                logger.error(
                    "Voice task failed for user %s: %s", user_id, exc, exc_info=True
                )

        task.add_done_callback(_on_done)

    async def _cancel_user_voice_tasks(self, user_id: int) -> int:
        tasks = self._prune_voice_tasks(user_id)
        cancelled = 0
        for task in list(tasks):
            if not task.done():
                task.cancel()
                cancelled += 1
        if tasks:
            await asyncio.gather(*list(tasks), return_exceptions=True)
        tasks.clear()
        self._runtime_active_voice_sessions.discard(user_id)
        return cancelled

    async def _cleanup_stale_audio_files(
        self, audio_dir: FilePath, max_age_seconds: int
    ) -> int:
        return await self._audio_processor.cleanup_stale_audio_files(
            audio_dir=audio_dir,
            max_age_seconds=max_age_seconds,
        )

    def _get_user_queue_lock(self, user_id: int) -> asyncio.Lock:
        lock = self._user_queue_locks.get(user_id)
        if lock is None:
            lock = asyncio.Lock()
            self._user_queue_locks[user_id] = lock
        return lock

    def _prune_user_tasks(self, user_id: int) -> set[asyncio.Task]:
        tasks = self._user_run_tasks.get(user_id)
        if not tasks:
            tasks = set()
            self._user_run_tasks[user_id] = tasks
            return tasks
        done = {t for t in tasks if t.done()}
        tasks.difference_update(done)
        return tasks

    def _track_user_task(self, user_id: int, task: asyncio.Task) -> None:
        tasks = self._prune_user_tasks(user_id)
        tasks.add(task)

        def _on_done(t: asyncio.Task):
            current = self._user_run_tasks.get(user_id)
            if current is not None:
                current.discard(t)
            try:
                t.result()
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.error(
                    f"Background task failed for user {user_id}: {e}", exc_info=True
                )

        task.add_done_callback(_on_done)

    def _clear_user_queue(self, user_id: int) -> int:
        tasks = self._prune_user_tasks(user_id)
        cleared = len(tasks)
        for t in list(tasks):
            t.cancel()
        tasks.clear()
        return cleared

    async def _enqueue_user_task(
        self,
        user_id: int,
        run_task: Callable[[], Awaitable[None]],
        on_overflow: Callable[[], Awaitable[None]],
    ) -> bool:
        lock = self._get_user_queue_lock(user_id)
        accepted_task: Optional[asyncio.Task] = None

        async with lock:
            tasks = self._prune_user_tasks(user_id)
            if len(tasks) >= self._MAX_INFLIGHT_MESSAGES:
                accepted_task = None
            else:
                # Wrap run_task to track active task execution
                async def wrapped_task():
                    # Store as active task when execution starts
                    current_task = asyncio.current_task()
                    self._active_tasks[user_id] = current_task
                    try:
                        await run_task()
                    except asyncio.CancelledError:
                        # Re-raise to ensure cancellation propagates
                        raise
                    finally:
                        # Remove from active tasks when done
                        self._active_tasks.pop(user_id, None)

                accepted_task = asyncio.create_task(wrapped_task())
                self._track_user_task(user_id, accepted_task)

        if not accepted_task:
            await on_overflow()
            return False
        return True

    async def _cmd_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /command xxx - forward as Claude Code slash command"""
        if not await self._check_access(update):
            return
        text = update.message.text
        user_id = update.effective_user.id
        log_debug(user_id, "command", text)
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            reply = "Usage: /command <command_name> [args]\nExample: /command commit"
            await update.message.reply_text(reply)
            log_debug(user_id, "bot", reply)
            return

        slash_cmd = "/" + parts[1]

        async def run_task():
            session = await session_manager.get_session(user_id)
            try:
                await update.message.chat.send_action(action="typing")
            except Exception:
                pass
            response = await project_chat_handler.process_message(
                user_message=slash_cmd,
                user_id=user_id,
                chat_id=update.effective_chat.id,
                session_id=self._effective_session_id(user_id, session),
                model=session.get("model"),
                permission_callback=self._permission_callback,
                typing_callback=lambda: update.message.chat.send_action(
                    action="typing"
                ),
                bot=self.application.bot,
            )
            await self._save_session_id(user_id, response)
            await self._reply_smart(
                update.message,
                response.content,
                parse_mode="Markdown",
                force_options=response.has_options,
                streamed=response.streamed,
            )

        async def on_overflow():
            reply = "⏳ Processing previous messages, please wait or send /stop to terminate."
            await update.message.reply_text(reply)
            log_debug(user_id, "bot", reply)

        await self._enqueue_user_task(user_id, run_task, on_overflow)

    _BUILTIN_COMMANDS = {
        "start",
        "skills",
        "new",
        "model",
        "resume",
        "stop",
        "history",
        "command",
        "skill",
    }

    async def _exec_slash_command(self, update: Update, slash_cmd: str):
        """Execute a slash command via Claude Code CLI and reply."""
        user_id = update.effective_user.id

        async def run_task():
            session = await session_manager.get_session(user_id)
            await update.message.chat.send_action(action="typing")
            try:
                response = await project_chat_handler.process_message(
                    user_message=slash_cmd,
                    user_id=user_id,
                    chat_id=update.effective_chat.id,
                    session_id=self._effective_session_id(user_id, session),
                    model=session.get("model"),
                    permission_callback=self._permission_callback,
                    typing_callback=lambda: update.message.chat.send_action(
                        action="typing"
                    ),
                    bot=self.application.bot,
                )
                await self._save_session_id(user_id, response)
                await self._reply_smart(
                    update.message,
                    response.content,
                    parse_mode="Markdown",
                    force_options=response.has_options,
                    streamed=response.streamed,
                )
            except Exception as e:
                logger.error(f"Skill execution failed: {e}", exc_info=True)
                await update.message.reply_text(f"❌ Execution failed: {str(e)}")

        async def on_overflow():
            reply = "⏳ Processing previous messages, please wait or send /stop to terminate."
            await update.message.reply_text(reply)
            log_debug(user_id, "bot", reply)

        await self._enqueue_user_task(user_id, run_task, on_overflow)

    async def _cmd_skill(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /skill xxx [args] - forward as Claude Code slash command (/xxx [args])"""
        if not await self._check_access(update):
            return
        text = update.message.text
        user_id = update.effective_user.id
        log_debug(user_id, "command", text)
        parts = text.split(maxsplit=2)  # /skill, name, args
        if len(parts) < 2:
            reply = "Usage: /skill <skill_name> [args]\nExample: /skill post-url-to-x https://example.com"
            await update.message.reply_text(reply)
            log_debug(user_id, "bot", reply)
            return

        skill_name = parts[1]
        args = parts[2] if len(parts) > 2 else ""
        await self._exec_slash_command(update, f"/{skill_name} {args}".strip())

    async def _handle_skill_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ):
        """Handle skill commands like /baoyu-post-to-x - forward to Claude Code CLI"""
        if not await self._check_access(update):
            return
        if not update.message or not update.message.text:
            return

        text = update.message.text
        parts = text.split(maxsplit=1)
        command = parts[0]

        # Skip commands already handled by explicit CommandHandlers
        cmd_name = command.lstrip("/").split("@")[0]
        if cmd_name in self._BUILTIN_COMMANDS:
            return
        args = parts[1] if len(parts) > 1 else ""

        user_id = update.effective_user.id
        log_debug(user_id, "command", text)

        await self._exec_slash_command(update, f"/{cmd_name} {args}".strip())

    @staticmethod
    def _resolve_voice_extension(mime_type: Optional[str]) -> str:
        if not mime_type:
            return "ogg"
        normalized = mime_type.lower()
        if "amr" in normalized:
            return "amr"
        if "mp3" in normalized or "mpeg" in normalized:
            return "mp3"
        if "wav" in normalized:
            return "wav"
        if "m4a" in normalized or "mp4" in normalized:
            return "m4a"
        return "ogg"

    @staticmethod
    def _build_voice_file_name(user_id: int, extension: str) -> str:
        timestamp_ms = int(time.time() * 1000)
        return f"{user_id}_{timestamp_ms}.{extension}"

    def _get_whisper_transcriber(self) -> WhisperTranscriber:
        if self._whisper_transcriber is None:
            self._whisper_transcriber = WhisperTranscriber(
                api_key=config.openai_api_key,
                model=config.whisper_model,
                base_url=config.openai_base_url,
            )
        return self._whisper_transcriber

    async def _download_voice_file(self, voice, destination: FilePath) -> None:
        telegram_file = await self.application.bot.get_file(voice.file_id)
        logger.debug("Downloading voice file to %s", destination)
        if hasattr(telegram_file, "download_to_drive"):
            await telegram_file.download_to_drive(custom_path=str(destination))
            return
        if hasattr(self.application.bot, "download_file"):
            await self.application.bot.download_file(
                telegram_file.file_path, custom_path=str(destination)
            )
            return
        raise RuntimeError("Telegram file download API is unavailable.")

    async def _prepare_audio_for_whisper(
        self, source_path: FilePath, cleanup_paths: List[FilePath]
    ) -> FilePath:
        detected_format = await self._audio_processor.detect_audio_format(source_path)
        logger.debug("Detected voice format %s for %s", detected_format, source_path)

        if detected_format == "mp3":
            return source_path

        if detected_format not in {"amr", "ogg"}:
            return source_path

        ffmpeg_ready = await self._audio_processor.check_ffmpeg_available()
        if not ffmpeg_ready:
            raise RuntimeError(
                "ffmpeg is not installed. Install ffmpeg and retry voice message processing."
            )

        converted_path = source_path.with_suffix(".mp3")
        cleanup_paths.append(converted_path)
        converted = await self._audio_processor.convert_audio(
            source_path, converted_path
        )
        return converted

    async def _process_user_message_text(
        self, update: Update, user_id: int, text: str
    ) -> None:
        current_session = await session_manager.get_session(user_id)
        try:
            await update.message.chat.send_action(action="typing")
        except Exception:
            pass

        try:
            new_session = current_session.pop("new_session", False)
            if new_session:
                await session_manager.update_session(user_id, current_session)

            response = await project_chat_handler.process_message(
                user_message=text,
                user_id=user_id,
                chat_id=update.effective_chat.id,
                message_id=update.message.message_id,
                session_id=self._effective_session_id(user_id, current_session),
                model=current_session.get("model"),
                new_session=new_session,
                permission_callback=self._permission_callback,
                typing_callback=lambda: update.message.chat.send_action(
                    action="typing"
                ),
                bot=self.application.bot,
            )
            await self._save_session_id(user_id, response)
            await self._reply_smart(
                update.message,
                response.content,
                parse_mode="Markdown",
                force_options=response.has_options,
                streamed=response.streamed,
            )
        except asyncio.CancelledError:
            # Task was cancelled by /stop command - silently exit
            # The /stop handler will send the user response
            logger.debug(f"Message processing cancelled for user {user_id}")
            raise
        except Exception as e:
            logger.error(f"Error in project chat: {e}", exc_info=True)
            await update.message.reply_text(
                "❌ Sorry, an error occurred while processing your message.\n"
                f"Error: {str(e)}\n\n"
                "Please try again later."
            )

    async def _handle_voice_message(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ):
        del context
        if not await self._check_access(update):
            return
        if not update.message or not update.message.voice:
            return

        user_id = update.effective_user.id
        voice = update.message.voice
        log_debug(user_id, "voice", f"voice:{voice.file_id} duration={voice.duration}")

        async def run_task():
            task = asyncio.current_task()
            if task is not None:
                self._track_voice_task(user_id, task)

            self._audio_dir.mkdir(parents=True, exist_ok=True)
            start = time.perf_counter()
            cleanup_paths: List[FilePath] = []
            outcome = "failed"

            try:
                if voice.duration and voice.duration > config.max_voice_duration:
                    await update.message.reply_text(
                        f"❌ Voice message is too long. Max duration is {config.max_voice_duration} seconds."
                    )
                    outcome = "duration_limit_exceeded"
                    return

                extension = self._resolve_voice_extension(
                    getattr(voice, "mime_type", None)
                )
                file_name = self._build_voice_file_name(
                    user_id=user_id, extension=extension
                )
                source_path = self._audio_dir / file_name
                cleanup_paths.append(source_path)
                logger.debug("Voice temp file path: %s", source_path)

                try:
                    await self._download_voice_file(voice, source_path)
                except Exception as exc:
                    logger.error(
                        "Voice file download failed for user %s: %s",
                        user_id,
                        exc,
                        exc_info=True,
                    )
                    await update.message.reply_text(
                        "❌ Failed to download your voice message. Please retry."
                    )
                    outcome = "download_failed"
                    return

                try:
                    audio_path = await self._prepare_audio_for_whisper(
                        source_path, cleanup_paths
                    )
                except Exception as exc:
                    logger.error(
                        "Voice conversion failed for user %s: %s",
                        user_id,
                        exc,
                        exc_info=True,
                    )
                    await update.message.reply_text(
                        "❌ Failed to convert audio for transcription. "
                        "Please ensure ffmpeg is installed and try again."
                    )
                    outcome = "conversion_failed"
                    return

                try:
                    transcriber = self._get_whisper_transcriber()
                except ValueError:
                    await update.message.reply_text(
                        "❌ Voice transcription is not configured. Please set OPENAI_API_KEY."
                    )
                    outcome = "missing_openai_key"
                    return

                try:
                    text = await transcriber.transcribe_audio(
                        audio_path, duration_seconds=voice.duration
                    )
                except EmptyTranscriptionError:
                    await update.message.reply_text(
                        "❌ No speech was detected in your voice message. Please try again."
                    )
                    outcome = "empty_transcription"
                    return
                except TranscriptionError as exc:
                    logger.error(
                        "Whisper transcription failed for user %s: %s", user_id, exc
                    )
                    await update.message.reply_text(
                        "❌ Failed to transcribe your voice message. Please try again later."
                    )
                    outcome = "transcription_failed"
                    return

                preview = f"🎤 Voice: {text}"
                await update.message.reply_text(preview)
                await self._process_user_message_text(update, user_id, text)
                outcome = "success"
            except asyncio.CancelledError:
                outcome = "cancelled"
                logger.info("Voice processing cancelled for user %s", user_id)
                raise
            finally:
                await self._audio_processor.cleanup_audio_files(cleanup_paths)
                elapsed_ms = int((time.perf_counter() - start) * 1000)
                logger.info(
                    "Voice processing result user_id=%s duration=%s outcome=%s elapsed_ms=%s",
                    user_id,
                    voice.duration,
                    outcome,
                    elapsed_ms,
                )

        async def on_overflow():
            reply = (
                f"⏳ Voice queue is full ({self._MAX_INFLIGHT_MESSAGES} active tasks). "
                "Please wait or send /stop to terminate running tasks."
            )
            await update.message.reply_text(reply)
            log_debug(user_id, "bot", reply)

        await self._enqueue_user_task(user_id, run_task, on_overflow)

    async def _handle_text_message(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ):
        """Handle text messages - use project chat or answer pending questions"""
        if not await self._check_access(update):
            return
        if not update.message or not update.message.text:
            return

        user_id = update.effective_user.id
        text = update.message.text
        session = await session_manager.get_session(user_id)

        # Check resume selection (user replies with a number)
        resume_list = session.get("resume_list")
        if resume_list and text.strip().isdigit():
            log_debug(user_id, "user", text)
            idx = int(text.strip()) - 1
            if 0 <= idx < len(resume_list):
                sid, msg = resume_list[idx]
                session["session_id"] = sid
                session["new_session"] = False
                session.pop("resume_list", None)
                await session_manager.update_session(user_id, session)
                self._runtime_active_sessions.add(user_id)
                reply = f"✅ Switched to session: {msg}"
                await update.message.reply_text(reply)
                log_debug(user_id, "bot", reply)
                # Send last assistant message as progress summary
                last_msg = project_chat_handler.get_session_last_assistant_message(sid)
                if last_msg:
                    progress = f"📋 {last_msg}"
                    await update.message.reply_text(progress)
                    log_debug(user_id, "bot", progress)
                return
            else:
                reply = "❌ Invalid number, please try again."
                await update.message.reply_text(reply)
                log_debug(user_id, "bot", reply)
                return

        # Clear resume list if user sends non-number
        if resume_list:
            session.pop("resume_list", None)
            await session_manager.update_session(user_id, session)

        # Capture explicit outside-path approval/denial from user replies.
        await self._maybe_capture_outside_approval(user_id, text)

        # Check if there's a pending question
        pending = await session_manager.get_pending_question(user_id)
        if pending:
            log_debug(user_id, "user", f"[answer] {text}")
            await session_manager.clear_pending_question(user_id)
            reply = f"✅ Answer received: {text}\n\nContinuing..."
            await update.message.reply_text(reply)
            log_debug(user_id, "bot", reply)
            return

        async def run_task():
            await self._process_user_message_text(update, user_id, text)

        async def on_overflow():
            reply = "⏳ Processing previous messages, please wait or send /stop to terminate."
            await update.message.reply_text(reply)
            log_debug(user_id, "bot", reply)

        await self._enqueue_user_task(user_id, run_task, on_overflow)

    # Match both absolute (/foo/bar.png) and relative (foo/bar.png) file paths
    _FILE_PATH_RE = re.compile(
        r"(/?(?:[\w.@-]+/)+[\w.@-]+\.(?:png|jpg|jpeg|gif|webp|mp4|mp3|pdf|zip))",
        re.IGNORECASE,
    )
    _IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
    _OPTION_RE = re.compile(r"^\s*(\d+)[.、)）]\s*(.+)", re.MULTILINE)

    def _resolve_paths(self, content: str) -> List[FilePath]:
        """Extract file paths from text and resolve relative ones against PROJECT_ROOT."""
        from telegram_bot.core.project_chat import PROJECT_ROOT

        paths = []
        seen = set()
        for m in self._FILE_PATH_RE.findall(content):
            p = FilePath(m.strip())
            if not p.is_absolute():
                p = PROJECT_ROOT / p
            p = p.resolve()
            if p not in seen and p.is_file() and p.stat().st_size < 10 * 1024 * 1024:
                seen.add(p)
                paths.append(p)
        return paths

    def _split_paths_by_scope(
        self, paths: List[FilePath]
    ) -> Tuple[List[FilePath], List[FilePath]]:
        in_root: List[FilePath] = []
        outside: List[FilePath] = []
        for path in paths:
            if self._is_within_project_root(path):
                in_root.append(path)
            else:
                outside.append(path)
        return in_root, outside

    def _extract_options(self, text: str) -> List[str]:
        """Extract numbered options from text like '1. xxx\n2. xxx'."""
        matches = self._OPTION_RE.findall(text)
        if len(matches) < 2:
            return []
        # Verify consecutive numbering starting from 1
        nums = [int(m[0]) for m in matches]
        if nums != list(range(1, len(nums) + 1)):
            return []
        return [m[1].strip() for m in matches]

    def _build_option_keyboard(
        self, options: List[str]
    ) -> Optional[InlineKeyboardMarkup]:
        """Build inline keyboard from extracted options."""
        if not options:
            return None
        buttons = []
        for i, opt in enumerate(options, 1):
            # callback_data max 64 bytes; truncate label if needed
            label = f"{i}. {opt}"
            cb_data = f"opt:{label}"
            if len(cb_data.encode("utf-8")) > 64:
                cb_data = f"opt:{i}"
            buttons.append([InlineKeyboardButton(label, callback_data=cb_data)])
        return InlineKeyboardMarkup(buttons)

    async def _send_file_paths(self, chat_id: int, paths: List[FilePath]) -> None:
        bot = self.application.bot
        for p in paths:
            try:
                if p.suffix.lower() in self._IMAGE_EXTS:
                    with open(p, "rb") as f:
                        await bot.send_photo(chat_id, photo=f)
                else:
                    with open(p, "rb") as f:
                        await bot.send_document(chat_id, document=f)
            except Exception as e:
                logger.warning(f"Failed to send file {p}: {e}")

    async def _prompt_outside_file_confirmation(
        self, chat_id: int, user_id: int, paths: List[FilePath]
    ) -> None:
        session = await session_manager.get_session(user_id)
        session["pending_external_files"] = [str(p) for p in paths]
        await session_manager.update_session(user_id, session)
        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "✅ Send external files", callback_data="extsend:allow"
                    )
                ],
                [InlineKeyboardButton("❌ Cancel", callback_data="extsend:deny")],
            ]
        )
        await self.application.bot.send_message(
            chat_id,
            "File paths outside PROJECT_ROOT detected. Confirmation required before sending.",
            reply_markup=kb,
        )

    @staticmethod
    def _split_text(text: str, limit: int = 4000) -> List[str]:
        """Split text into chunks no longer than limit, breaking at paragraph or line boundaries."""
        if len(text) <= limit:
            return [text]
        chunks: List[str] = []
        remaining = text
        while len(remaining) > limit:
            # Try to split at a paragraph boundary (double newline)
            cut = remaining.rfind("\n\n", 0, limit)
            if cut == -1:
                # Fall back to single newline
                cut = remaining.rfind("\n", 0, limit)
            if cut == -1:
                # Hard cut at limit
                cut = limit
            else:
                cut += 1  # include the newline in the current chunk
            chunks.append(remaining[:cut].rstrip())
            remaining = remaining[cut:].lstrip("\n")
        if remaining:
            chunks.append(remaining)
        return chunks

    async def _reply_smart(
        self,
        message,
        content: str,
        parse_mode: str = "Markdown",
        force_options: bool = False,
        streamed: bool = False,
    ):
        """Reply with text (splitting if needed), send referenced files, and add option buttons."""
        # Skip text sending if already streamed
        if not streamed:
            for part in self._split_text(content):
                try:
                    await message.reply_text(part, parse_mode=parse_mode)
                except Exception:
                    await message.reply_text(part)

        # Send files mentioned in the response
        resolved_paths = self._resolve_paths(content)
        in_root_paths, _ = self._split_paths_by_scope(resolved_paths)
        await self._send_file_paths(message.chat.id, in_root_paths)

        # Only show inline keyboard for AskUserQuestion degraded content
        if force_options:
            options = self._extract_options(content)
            kb = self._build_option_keyboard(options)
            if kb:
                await message.reply_text("Please select:", reply_markup=kb)

    async def _send_smart(
        self,
        chat_id: int,
        content: str,
        user_id: Optional[int] = None,
        force_options: bool = False,
        streamed: bool = False,
    ):
        """Send text to chat_id (splitting if needed) with file and option detection."""
        bot = self.application.bot

        # Skip text sending if already streamed
        if not streamed:
            for part in self._split_text(content):
                try:
                    await bot.send_message(chat_id, part, parse_mode="Markdown")
                except Exception:
                    await bot.send_message(chat_id, part)

        resolved_paths = self._resolve_paths(content)
        in_root_paths, _ = self._split_paths_by_scope(resolved_paths)
        await self._send_file_paths(chat_id, in_root_paths)
        # Only show inline keyboard for AskUserQuestion degraded content
        if force_options:
            options = self._extract_options(content)
            kb = self._build_option_keyboard(options)
            if kb:
                await bot.send_message(chat_id, "Please select:", reply_markup=kb)

    async def _handle_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ):
        """Handle callback queries from inline keyboards"""
        if not await self._check_access(update):
            return

        query = update.callback_query
        await query.answer()

        user_id = update.effective_user.id
        data = query.data

        if data.startswith("extsend:"):
            session = await session_manager.get_session(user_id)
            pending = session.get("pending_external_files", [])
            session.pop("pending_external_files", None)
            await session_manager.update_session(user_id, session)

            if data == "extsend:deny":
                await query.edit_message_text("❌ External file sending cancelled.")
                return

            if not pending:
                await query.edit_message_text("ℹ️ No pending external files.")
                return

            await query.edit_message_text("✅ Confirmed. Sending external files...")
            paths: List[FilePath] = []
            for raw in pending:
                p = FilePath(raw)
                try:
                    resolved = p.resolve(strict=False)
                    if (
                        resolved.is_file()
                        and resolved.stat().st_size < 10 * 1024 * 1024
                    ):
                        paths.append(resolved)
                except Exception:
                    continue
            await self._send_file_paths(update.effective_chat.id, paths)
            return

        # Handle permission request buttons
        # Handle numbered option buttons (from Claude's text-based choices)
        if data.startswith("opt:"):
            choice = data.split(":", 1)[1]
            await query.edit_message_text(f"✅ Selected: {choice}")
            # Send choice back to Claude as a new message
            chat_id = update.effective_chat.id
            await self._maybe_capture_outside_approval(user_id, choice)

            async def run_task():
                session = await session_manager.get_session(user_id)
                await self.application.bot.send_chat_action(chat_id, action="typing")
                try:
                    response = await project_chat_handler.process_message(
                        user_message=choice,
                        user_id=user_id,
                        chat_id=chat_id,
                        session_id=self._effective_session_id(user_id, session),
                        model=session.get("model"),
                        permission_callback=self._permission_callback,
                        typing_callback=lambda: self.application.bot.send_chat_action(
                            chat_id, action="typing"
                        ),
                        bot=self.application.bot,
                    )
                    await self._save_session_id(user_id, response)
                    await self._send_smart(
                        chat_id,
                        response.content,
                        user_id=user_id,
                        force_options=response.has_options,
                        streamed=response.streamed,
                    )
                except Exception as e:
                    logger.error(f"Option reply failed: {e}", exc_info=True)
                    await self.application.bot.send_message(
                        chat_id, f"❌ Processing failed: {e}"
                    )

            async def on_overflow():
                await self.application.bot.send_message(
                    chat_id,
                    "⏳ Processing previous messages, please wait or send /stop to terminate.",
                )

            await self._enqueue_user_task(user_id, run_task, on_overflow)
            return

        # Handle model selection
        if data.startswith("model:"):
            model_name = data.split(":", 1)[1]
            log_debug(user_id, "callback", f"model:{model_name}")
            session = await session_manager.get_session(user_id)
            session["model"] = model_name
            await session_manager.update_session(user_id, session)
            label = dict(self.MODELS).get(model_name, model_name)
            logger.info(
                f"User {user_id}: model set to {model_name!r} via inline keyboard"
            )
            reply = f"✅ Model switched to: {label}"
            await query.edit_message_text(reply)
            log_debug(user_id, "bot", reply)
            return

        # Check if there's a pending question
        pending = await session_manager.get_pending_question(user_id)
        if pending:
            await session_manager.clear_pending_question(user_id)
            await query.edit_message_text(f"✅ Selected: {data}\n\nContinuing...")

    async def _set_bot_commands(self):
        """Set bot commands menu"""
        commands = [
            BotCommand("new", "Start a new Claude Code session"),
            BotCommand("stop", "Interrupt execution immediately"),
            BotCommand("model", "Switch Claude Code model"),
            BotCommand("resume", "Resume a previous session"),
            BotCommand("history", "View recent conversation history"),
            BotCommand("skills", "View available skills"),
            BotCommand("skill", "Execute a Claude Code skill"),
            BotCommand("command", "Execute a Claude Code command"),
        ]
        for scope in (
            BotCommandScopeAllPrivateChats(),
            BotCommandScopeAllGroupChats(),
            BotCommandScopeAllChatAdministrators(),
        ):
            await self.application.bot.set_my_commands(commands, scope=scope)
        logger.info("Bot commands set")


bot = TelegramBot()
