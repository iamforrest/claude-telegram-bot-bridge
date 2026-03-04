---
name: setup
description: Install and configure the Telegram Skill Bot. Use when users ask to install, set up, or configure the bot. Supports multi-language installation guidance (English, Chinese, Japanese, Spanish, French, German). Handles system requirement checks, bot token collection, user whitelist configuration, proxy setup, virtual environment creation, dependency installation, and .env file generation.
allow_tools:
  - Read
  - Bash(md5 *)
---

# Install Telegram Skill Bot

This skill guides users through installing the Telegram Skill Bot in their preferred language.

## What this skill does

1. Detects the user's language from their message
2. Checks system requirements (Python 3.11+, Claude CLI)
3. Guides the user to get a Telegram bot token from @BotFather
4. Collects optional configuration (user whitelist, proxy)
5. Creates virtual environment and installs dependencies
6. Saves configuration to .env file
7. Optionally configures voice message transcription (OpenAI Whisper)
8. Provides next steps in the user's language

## Usage

Users can invoke this skill in any language:

```
/install
```

Or naturally:
```
帮我安装这个 Telegram bot
Help me install this bot
インストールを手伝ってください
Ayúdame a instalar este bot
```

## Implementation

### Step 1: Detect Language

Detect the user's language from their message. Default to English if unclear.

### Step 2: Check System Requirements

Check if the following are installed:
- Python 3.11 or higher (`python3 --version`)
- Claude CLI (`claude --version`)

If missing, provide installation instructions in the user's language:
- Python: https://www.python.org/downloads/ or `brew install python@3.11`
- Claude CLI: `npm install -g @anthropic-ai/claude-code` or `brew install anthropics/claude/claude`

### Step 3: Get Telegram Bot Token

Guide the user to create a Telegram bot:

1. Open Telegram and search for `@BotFather`
2. Send `/newbot` command
3. Follow instructions to create the bot
4. Copy the token (format: `123456789:ABCdefGHIjklMNOpqrsTUVwxyz`)

Ask the user to paste their bot token. Validate the format (should match: `^\d+:[A-Za-z0-9_-]+$`).

### Step 4: Optional Configuration

**User Whitelist (Optional):**
- Ask if they want to restrict access to specific Telegram users
- If yes, guide them to get their user ID from `@userinfobot`
- Accept comma-separated user IDs (e.g., `123456789,987654321`)
- If empty, all users can access the bot

**Proxy (Optional):**
- Ask if they need a proxy to access Telegram/Claude API
- If yes, accept HTTP proxy URL (e.g., `http://127.0.0.1:7890`)
- Validate URL format

### Step 5: Create Virtual Environment

```bash
python3 -m venv venv
```

### Step 6: Install Dependencies

```bash
venv/bin/pip install -q --upgrade pip
venv/bin/pip install -q -r requirements.txt
```

Save requirements hash for future checks:
```bash
md5 -q requirements.txt > venv/.req_hash
```

### Step 7: Save Configuration

Create `.env` file based on `.env.example` template with the collected information:

1. Read `.env.example` as the base template
2. Replace the following values with user-provided configuration:
   - `TELEGRAM_BOT_TOKEN=your_bot_token_here` → user's actual token
   - `# ALLOWED_USER_IDS=123456789,987654321` → uncomment and set if user provided IDs
   - `# PROXY_URL=http://127.0.0.1:7890` → uncomment and set if user provided proxy

3. Keep all other configuration options from `.env.example` as commented defaults
4. This ensures users have a complete reference of all available options

The generated `.env` should maintain the same structure and comments as `.env.example`, only activating the options the user explicitly configured.

### Step 8: Optional Voice Message Support

After saving the basic configuration, ask the user if they want to enable voice message transcription:

**Voice Message Support (Optional):**
- Explain that the bot can transcribe voice messages using OpenAI Whisper API
- If the user wants this feature, collect:
  - `OPENAI_API_KEY`: Required for Whisper API access
  - `OPENAI_BASE_URL`: Optional, for OpenAI-compatible endpoints (default: OpenAI official)
  - `WHISPER_MODEL`: Optional, model name (default: `whisper-1`)
  - `MAX_VOICE_DURATION`: Optional, max duration in seconds (default: 300)

If the user chooses to enable voice support, update the `.env` file by uncommenting and setting these variables:
- Uncomment `# OPENAI_API_KEY=sk-...` and set the actual API key
- Optionally uncomment and set `OPENAI_BASE_URL`, `WHISPER_MODEL`, `MAX_VOICE_DURATION` if user provides custom values

Note: All voice-related configuration options are already present in `.env` as commented defaults from Step 7.

### Step 9: Completion

Inform the user in their language that installation is complete. Provide next steps:

1. Start the bot:
   ```bash
   ./start.sh --path /path/to/your/project
   ```

2. Available options:
   - `-d, --daemon`: Run in background
   - `--debug`: Enable debug logging
   - `--status`: Check if bot is running
   - `--stop`: Stop the bot

3. Verify in Telegram:
   - Open Telegram and find your bot
   - Send `/start` to begin chatting with Claude
   - If voice support is enabled, try sending a voice message

## Error Handling

- If Python version is too old, provide upgrade instructions
- If Claude CLI is missing, provide installation instructions
- If token format is invalid, ask again with format example
- If venv creation fails, check Python installation
- If dependency installation fails, check network connection

## Multi-language Support

Provide all prompts and messages in the user's detected language. Support at least:
- English
- 简体中文 (Simplified Chinese)
- 日本語 (Japanese)
- Español (Spanish)
- Français (French)
- Deutsch (German)

## Notes

- This skill replaces the need to run `./setup.sh` manually
- It provides a more conversational, language-friendly installation experience
- Users can ask questions during installation and get help in their language
- The skill should be patient and guide users step-by-step
