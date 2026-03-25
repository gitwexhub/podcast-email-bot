# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Run the bot
source .venv/bin/activate
python -m src.bot

# Run tests (use venv python directly — system pytest won't have dependencies)
venv/bin/python -m pytest tests/ -v

# Lint / format
ruff check src/
ruff format src/
```

Tests use `asyncio_mode="auto"` (configured in `pyproject.toml`) — all test functions can be `async`.

---

## Architecture

```
┌─────────────────┐     ┌──────────────────────┐     ┌─────────────────┐
│  Telegram Bot   │────▶│   Processing Layer   │────▶│  JSON Storage   │
│  (Interface)    │     │                      │     │  (Summaries +   │
└─────────────────┘     │  - yt-dlp            │     │   Categories)   │
                        │  - feedparser        │     └─────────────────┘
                        │  - ffmpeg (compress) │              │
                        └──────────────────────┘              ▼
                                    │                 ┌─────────────────┐
                                    ▼                 │   AI Layer      │
                        ┌──────────────────────┐      │  (Claude API)   │
                        │  Transcription       │      │  - Summaries    │
                        │  - Groq (cloud)      │      │  - Refinement   │
                        │  - OpenAI (cloud)    │      │  - Categorize   │
                        │  - faster-whisper    │      │  - Reorganize   │
                        └──────────────────────┘      │  - Search       │
                                                      └─────────────────┘
                                                              │
                                                              ▼
                                                      ┌─────────────────┐
                                                      │ Learning System │
                                                      │ (Preferences)   │
                                                      └─────────────────┘
```

### Tech Stack
- **Language**: Python 3.11+
- **Bot Framework**: python-telegram-bot (async)
- **Transcription**: Groq Whisper API (cloud, free), OpenAI Whisper API, or faster-whisper (local)
- **Audio Extraction**: yt-dlp, feedparser, ffmpeg
- **AI**: Claude API via Anthropic SDK
- **Email**: Resend API (or SMTP)
- **Config**: Pydantic models with YAML + env var loading
- **Deployment**: Docker on Railway

---

## Configuration

Config loads from `config.yaml` if present, otherwise from env vars. Env vars always override file values. All values are `.strip()`'d to handle copy-paste whitespace.

### Required Variables

| Variable | Description |
|----------|-------------|
| `TELEGRAM_BOT_TOKEN` | Bot token from @BotFather |
| `TELEGRAM_ALLOWED_USERS` | Comma-separated Telegram user IDs |
| `ANTHROPIC_API_KEY` | Claude API key |
| `GROQ_API_KEY` | Groq Whisper key (or `OPENAI_API_KEY` with `gsk_` prefix) |

### Optional Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `OPENAI_WHISPER_KEY` | — | Real OpenAI key for auto-fallback on Groq 429/413 |
| `WHISPER_MODE` | `cloud` | `cloud` or `local` |
| `VAULT_PATH` | `./data` | Data storage path (Docker: `/data/vault`) |
| `AI_MODEL` | `claude-sonnet-4-20250514` | Claude model |
| `RESEND_API_KEY` | — | Email service |
| `EMAIL_ENABLED` | `false` | Enable email features |
| `DIGEST_TIME` | `20:00` | Daily digest time |
| `DIGEST_TIMEZONE` | `America/Los_Angeles` | Digest timezone |

### Groq Key Detection (`src/config.py`)
`_get_groq_key()` checks:
1. `GROQ_API_KEY` env var
2. `OPENAI_API_KEY` starting with `gsk_` (backward compat for Railway)

---

## Bot Modes

### AI-Only Mode
User sends `/podcast <url>` → bot transcribes and generates summary automatically → user reviews and approves/refines → saved to storage.

### Interactive "Highlighting" Mode
User selects interactive mode → adds highlights while listening (`/detail <text>`, `/insight <text>`) → sends `/end` → AI generates summary incorporating highlights → review/refine/save.

---

## Smart Folder System

Podcasts are auto-categorized on save. Every 5th save triggers an AI reorganization pass (merge near-duplicates, split folders >10 items).

### Data Model
- **`Category`** dataclass: `id`, `name`, `emoji`, `description`, `parent_id`, `summary_ids`, timestamps
- **Hierarchy**: Max 2 levels deep (parent → child). Enforced in `create_category()` and `move_category()`.
- **Storage**: `{vault_path}/.categories.json` with a `save_count` field for reorg triggering.

### Key Methods in `src/storage/categories.py`
- `list_tree()` — nested dict for display/AI context
- `apply_reorganization()` — batch operations from AI (merge/create/move/rename)
- `increment_save_count()` — tracks saves for reorg trigger

### Key Methods in `src/ai/summarizer.py`
- `categorize_summary(title, show_name, summary_text, folder_tree)` → folder path + create flag
- `reorganize_folders(folder_tree, summary_titles)` → list of operations
- `search_summaries(query, summary_list)` → ranked matches with relevance scores (1–5)

### Design Decisions
- **~20 folder cap**: AI prompt instructs Claude to reuse existing folders
- **Non-blocking categorization**: If categorization API call fails, the save still succeeds (logged as warning)
- **Backward compatible**: Existing summaries without `categories` field load with `categories: []`; use `/organize` to retroactively file them

---

## Common Bugs / Known Issues

### ConversationHandler Stuck State
Callbacks from background `asyncio.create_task` aren't caught by ConversationHandler. A standalone `CallbackQueryHandler` is registered outside the ConversationHandler (~line 1816 in `bot.py`). `conversation_timeout=600` provides auto-recovery.

### Groq 25MB File Limit
Auto-compressed with ffmpeg before sending. Very long podcasts (3+ hours) may still exceed the limit after compression.

### 429 Rate Limit on Groq Free Tier
If `OPENAI_WHISPER_KEY` is set, the bot automatically falls back to OpenAI. Without it, user must wait ~20 min.

### Tiny Last Chunk (400 "could not process file")
When a podcast is slightly over a 20-min boundary (e.g. 80min 30sec → 5 chunks where chunk 5 is 30sec), Groq/OpenAI reject the tiny final file. Fixed in `_split_audio`: any chunk under 30 seconds is skipped. Don't lower this threshold — even 20-second chunks can fail depending on audio encoding.

### Retry Logic Only Covers Transient Errors
The per-chunk retry (30s wait, 1 retry) only fires on `RateLimitError`, `APITimeoutError`, `APIConnectionError`, `InternalServerError`. It deliberately does NOT retry `BadRequestError` (400) — those are malformed files that will always fail and retrying just wastes 30 seconds before the same error.

### Spotify Episode Matching
The Spotify show ID extraction uses a `/show/([a-zA-Z0-9]{22})` regex on the episode page HTML. **Do not complicate this with multiple fallback methods** — that approach was tried and broke all podcast resolution.

### iTunes Returns Wrong Podcast for Generic Names
iTunes matching uses priority-based matching:
1. Exact match: `name == podcast_name` (case-insensitive)
2. Substring match: `podcast_name in name`
3. Best guess: first result with a feed URL

### Telegram Markdown Parse Errors
Special characters in summaries can cause `telegram.error.BadRequest` with MarkdownV2. Error messages are sent as plain text to avoid this.

---

## Learning System

Tracks user preferences to improve summaries over time: length, detail level, tone, features, and feedback patterns. Stored in `data/.learning.json`, injected into summarizer prompts.

---

## Commands Reference

| Command | Description |
|---------|-------------|
| `/podcast <url>` | Process a podcast |
| `/lookup` | Browse folders, search, and manage saved summaries |
| `/organize` | AI-powered folder reorganization or batch categorization |
| `/status` | Check processing queue and active sessions |
| `/stop` | Cancel all stuck processes and clear session state |
| `/stats` | View learning statistics |
| `/poweron` / `/poweroff` | Start/stop bot (supervisor mode) |
| `/cancel` | Cancel current operation |

**Interactive Mode:** `/detail <text>` (key fact), `/insight <text>` (takeaway), `/end` (generate summary)

---

## Adding New Content Types

1. Create processor in `src/processors/`
2. Add handler in `src/bot.py`
3. Update summarizer prompt in `src/ai/summarizer.py` if needed

## Deployment (Railway)

Dockerfile uses `python:3.11-slim` + ffmpeg with `requirements-cloud.txt` (no PyTorch/faster-whisper). Persistent data requires a Railway volume mount at `/data/vault`. `railway.toml` sets restart policy to ON_FAILURE (max 5 retries).
