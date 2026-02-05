# Podcast Knowledge Bot - Developer Documentation

## Product Overview

A Telegram bot that transforms podcast episodes into structured, email-ready summaries using Whisper transcription and Claude AI. Supports natural language refinement and learns from user feedback.

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

## Key Files

| File | Purpose |
|------|---------|
| `src/bot.py` | Main Telegram bot - handlers, conversation flows, email sending |
| `src/config.py` | Pydantic configuration models, env var loading, Groq key detection |
| `src/ai/summarizer.py` | Claude API integration for generating/refining summaries |
| `src/ai/learning.py` | Feedback tracking and preference learning system |
| `src/processors/podcast.py` | Audio extraction (yt-dlp), compression (ffmpeg), Whisper transcription |
| `src/storage/summaries.py` | JSON-based summary persistence |
| `src/storage/categories.py` | Hierarchical folder/category storage with tree operations |
| `Dockerfile` | Cloud build - python:3.11-slim + ffmpeg, uses requirements-cloud.txt |
| `railway.toml` | Railway deploy config (dockerfile builder, restart on failure) |
| `requirements.txt` | Full local deps (includes faster-whisper, sentence-transformers) |
| `requirements-cloud.txt` | Cloud deps (no PyTorch/faster-whisper for smaller image) |
| `.env.example` | Env var template for local dev or Railway |
| `config.yaml` | User configuration (gitignored - contains secrets) |
| `config.yaml.example` | YAML config template |

---

## Railway Deployment

### Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `TELEGRAM_BOT_TOKEN` | Yes | Bot token from @BotFather |
| `TELEGRAM_ALLOWED_USERS` | Yes | Comma-separated Telegram user IDs |
| `ANTHROPIC_API_KEY` | Yes | Claude API key |
| `GROQ_API_KEY` | Yes | Groq API key (or use `OPENAI_API_KEY` with gsk_ prefix) |
| `OPENAI_WHISPER_KEY` | No | Real OpenAI key for automatic fallback on Groq 429/413 |
| `WHISPER_MODE` | No | `cloud` (default) or `local` |
| `VAULT_PATH` | No | Data storage path (default: `/data/vault`) |
| `AI_MODEL` | No | Claude model (default: `claude-sonnet-4-20250514`) |
| `RESEND_API_KEY` | No | For email features |
| `EMAIL_ENABLED` | No | `true`/`false` |
| `DIGEST_TIME` | No | Daily digest time (default: `20:00`) |
| `DIGEST_TIMEZONE` | No | Timezone (default: `America/Los_Angeles`) |

### Groq Key Detection Logic (`src/config.py`)
The `_get_groq_key()` function checks:
1. `GROQ_API_KEY` env var (primary, documented)
2. `OPENAI_API_KEY` starting with `gsk_` (backward compat for Railway)

This means on Railway you can set either `GROQ_API_KEY=gsk_...` or `OPENAI_API_KEY=gsk_...` and Groq will be detected.

---

## Bot Modes

### AI-Only Mode
1. User sends `/podcast <url>`
2. Bot transcribes and generates summary automatically
3. User reviews and can approve or provide feedback
4. Summary saved to storage

### Interactive "Highlighting" Mode
1. User sends `/podcast <url>` and selects interactive mode
2. While listening, user adds highlights:
   - `/detail <text>` - Key facts, stats, points
   - `/insight <text>` - Personal takeaways, connections
3. User sends `/end` when done
4. AI generates summary incorporating user's highlights
5. Review, refine, save

---

## Smart Folder System

AI-managed hierarchical folder organization for podcast summaries. Podcasts are automatically categorized on save, and the folder structure evolves over time.

### How It Works

1. **Auto-categorization**: When a podcast is saved, Claude analyzes the title, show name, and summary content against the current folder tree, then files it into the best-matching folder (or creates a new one).
2. **Dynamic reorganization**: Every 5th save, Claude reviews the full folder tree and can merge near-duplicates, split overgrown folders (>10 items), or rename unclear folders.
3. **User-editable**: Users can create, rename, move, and delete folders via inline buttons in `/lookup`.
4. **Smart search**: `/lookup` supports natural language queries — first tries substring match on titles/shows, then falls back to Claude semantic search with relevance scoring.

### Data Model

- **`Category`** dataclass: `id`, `name`, `emoji`, `description`, `parent_id`, `summary_ids`, timestamps
- **Hierarchy**: Max 2 levels deep (parent → child). Enforced in `create_category()` and `move_category()`.
- **Storage**: `{vault_path}/.categories.json` — includes a `save_count` field for triggering reorganization.
- **Summary link**: Each `PodcastSummary` has a `categories: list[str]` field storing category IDs.

### Key Methods

**`src/storage/categories.py` — CategoryStorage:**
- `create_category()`, `rename_category()`, `move_category()`, `delete_category()` — CRUD
- `add_summary()`, `remove_summary()`, `move_summary()` — filing
- `list_tree()` — nested dict structure for display/AI context
- `find_by_name()` — fuzzy match for user input
- `format_tree_display()` — formatted string for Telegram
- `apply_reorganization()` — batch operations from AI (merge/create/move/rename)
- `increment_save_count()` — tracks saves for reorg trigger

**`src/ai/summarizer.py` — AI methods:**
- `categorize_summary(title, show_name, summary_text, folder_tree)` → returns folder path + create flag
- `reorganize_folders(folder_tree, summary_titles)` → returns list of operations
- `search_summaries(query, summary_list)` → returns ranked matches with relevance scores (1-5)

### Folder UI Flow (in `/lookup`)

```
/lookup → Library view (folder tree + recent 5)
  ├── Type number → View summary detail
  ├── Type folder name → Browse folder contents (paginated)
  │     ├── Sub-folders listed first
  │     ├── Summaries with pagination (10/page)
  │     └── Buttons: New Sub-folder, Rename, Move, Delete, Back
  └── Type text → Search (substring → semantic)
        └── Results with relevance stars
```

### Summary Actions (in detail view)
- Edit, Send as Email, **Move to Folder**, Delete, Back to Library

### `/organize` Command
- If no folders exist: batch-categorizes all existing podcasts
- If folders exist: triggers AI reorganization pass

### Design Decisions
- **~20 folder cap**: AI prompt instructs Claude to reuse existing folders and keep total manageable
- **Non-blocking categorization**: If categorization API call fails, the save still succeeds (logged as warning)
- **Backward compatible**: Existing summaries without `categories` field load with `categories: []`. Use `/organize` to retroactively file them.
- **Telegram message limits**: Folder tree display uses counts-only for sub-folders; pagination prevents exceeding 4096 char limit.

---

## Common Bugs / Known Issues

### ConversationHandler Stuck State
**Problem**: When callbacks are triggered from background tasks (`asyncio.create_task`), the ConversationHandler may not catch them, leaving the user stuck.
**Solution**: A standalone `CallbackQueryHandler` is registered outside the ConversationHandler (line ~1816 in bot.py). The ConversationHandler also has a `conversation_timeout=600` (10 minutes) to auto-recover.

### Groq 25MB File Limit
**Problem**: Groq's Whisper API rejects files over 25MB.
**Solution**: The podcast processor auto-compresses audio with ffmpeg before sending. Very long podcasts (3+ hours) may still exceed the limit after compression.

### Trailing Whitespace in API Keys
**Problem**: Copy-pasting API keys from web UIs sometimes includes trailing newlines or spaces, causing auth failures.
**Solution**: All env var reads in `config.py` call `.strip()` on the value.

### 429 Rate Limit on Groq Free Tier
**Problem**: Groq's free tier has per-hour audio limits (~2 hours/hour). Back-to-back podcast processing can hit this.
**Solution**: If `OPENAI_WHISPER_KEY` is set, the bot automatically falls back to OpenAI and notifies the user. Without it, the error surfaces and user must wait ~20 min.

### Telegram Markdown Parse Errors
**Problem**: Special characters in error messages or summaries can cause `telegram.error.BadRequest` when using MarkdownV2 parse mode.
**Solution**: The bot sanitizes output, but edge cases with nested formatting can slip through. Error messages are sent as plain text.

### Spotify Episode Matching (Wrong Episode)
**Problem**: When given a Spotify show link (not episode), yt-dlp may match the wrong episode.
**Solution**: Users should provide specific episode links. The bot extracts episode metadata from Spotify's page when possible.

### iTunes Returns Wrong Podcast for Generic Names
**Problem**: Podcasts with generic names like "Empire" can match the wrong podcast in iTunes search. For example, searching "Empire" returns "Empire: World History" before "Empire" (Blockworks), and substring matching (`"empire" in "empire: world history"`) matches the wrong one.
**Solution**: iTunes matching now uses priority-based matching:
1. **Exact match first**: `name == podcast_name` (case-insensitive)
2. **Substring match**: `podcast_name in name`
3. **Best guess**: First result with a feed URL

**Important**: The Spotify show ID extraction uses a simple `/show/([a-zA-Z0-9]{22})` regex on the episode page HTML. This pattern is reliable and should NOT be over-complicated with multiple fallback methods - that approach was tried and broke all podcast resolution.

---

## Learning System

Tracks user preferences to improve summaries over time:
- **Length**: brief / medium / detailed
- **Detail level**: high-level / balanced / granular
- **Tone**: casual / professional / academic
- **Features**: timestamps, soundbites, takeaways
- **Feedback patterns**: Common phrases the user uses

Preferences stored in `data/learning.json`, injected into summarizer prompts.

---

## Commands Reference

| Command | Description |
|---------|-------------|
| `/start` | Welcome message |
| `/help` | Show available commands |
| `/podcast <url>` | Process a podcast |
| `/lookup` | Browse folders, search, and manage saved summaries |
| `/organize` | AI-powered folder reorganization or batch categorization |
| `/status` | Check processing queue and active sessions |
| `/stop` | Cancel all stuck processes and clear session state |
| `/stats` | View learning statistics |
| `/poweron` / `/poweroff` | Start/stop bot (supervisor mode) |
| `/cancel` | Cancel current operation |

### Interactive Mode Commands
| Command | Description |
|---------|-------------|
| `/detail <text>` | Add a key fact or point |
| `/insight <text>` | Add a personal takeaway |
| `/end` | Generate summary with highlights |

---

## Security

- **Access control**: `allowed_users` whitelist in config
- **SSRF protection**: URL validation before fetching
- **Error sanitization**: No internal paths/details in user-facing errors
- **Secrets management**: All keys in gitignored config.yaml or env vars
- **No secrets in source**: `_get_groq_key()` reads from env only

---

## Development

### Running Locally
```bash
source .venv/bin/activate
python -m src.bot
```

### Adding New Content Types
1. Create processor in `src/processors/`
2. Add handler in `src/bot.py`
3. Update summarizer prompt if needed

### Modifying Summary Format
Edit the prompt in `src/ai/summarizer.py`. The learning system injects user preferences automatically.

### Testing Transcription
```python
from src.processors.podcast import PodcastProcessor
processor = PodcastProcessor(config)
transcript = await processor.transcribe("path/to/audio.mp3")
```
