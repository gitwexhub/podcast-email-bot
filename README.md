# Podcast Knowledge Bot

A Telegram bot that transforms podcast episodes into professional summaries using Whisper transcription and Claude AI.

Send a podcast link, get back a structured summary with key points, notable quotes, and actionable takeaways. Refine with natural language feedback, save, and email.

## Features

- **Cloud or local transcription** - Groq (free, fast), OpenAI, or local faster-whisper
- **AI-powered summaries** - Professional format via Claude with key points, soundbites, takeaways
- **Natural language refinement** - "Make it shorter", "Focus on the AI discussion"
- **Smart folder organization** - AI auto-categorizes podcasts into a hierarchical folder system that evolves over time
- **Natural language search** - Find past podcasts by topic, question, or keyword
- **Learning system** - Improves summaries based on your feedback over time
- **Email integration** - Send summaries to your inbox (Resend or SMTP)
- **Access control** - Whitelist-based, only authorized Telegram users

## Deployment Options

### Option A: Railway (Recommended for 24/7 access)

1. Fork this repo on GitHub

2. Create a new project on [Railway](https://railway.app) and connect your fork

3. Add these environment variables in Railway:

   | Variable | Required | Description |
   |----------|----------|-------------|
   | `TELEGRAM_BOT_TOKEN` | Yes | From [@BotFather](https://t.me/botfather) |
   | `TELEGRAM_ALLOWED_USERS` | Yes | Your Telegram user ID (from [@userinfobot](https://t.me/userinfobot)) |
   | `ANTHROPIC_API_KEY` | Yes | From [console.anthropic.com](https://console.anthropic.com) |
   | `GROQ_API_KEY` | Yes | From [console.groq.com](https://console.groq.com) (free tier) |
   | `OPENAI_WHISPER_KEY` | No | OpenAI key — auto-fallback when Groq is rate-limited ([get one](https://platform.openai.com/api-keys)) |
   | `WHISPER_MODE` | No | `cloud` (default) or `local` |
   | `VAULT_PATH` | No | `/data/vault` (default) |
   | `RESEND_API_KEY` | No | For email features |
   | `EMAIL_ENABLED` | No | `true` to enable email |

4. **Configure a persistent volume** (important!):
   - In your Railway project, go to your service
   - Click **Settings** → **Volumes**
   - Add a volume with mount path: `/data`
   - This ensures your saved podcasts and folders persist across deploys

5. Deploy - Railway auto-builds from the Dockerfile

   That's it! The bot will use Groq for fast free transcription, with OpenAI as automatic fallback if you added `OPENAI_WHISPER_KEY`.

   **Note:** Without the volume configured in step 4, you'll lose all saved podcasts every time the bot redeploys.

### Option B: Run Locally

1. **Prerequisites**: Python 3.11+, FFmpeg (`brew install ffmpeg`)

2. **Install**:
   ```bash
   git clone https://github.com/YOUR_USERNAME/knowledge-bot.git
   cd knowledge-bot
   python -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   ```

3. **Configure** (pick one):
   ```bash
   # Option A: YAML config file
   cp config.yaml.example config.yaml
   # Edit config.yaml with your API keys

   # Option B: Environment variables
   cp .env.example .env
   # Edit .env with your API keys, then: source .env
   ```

4. **Run**:
   ```bash
   python -m src.bot
   ```

   To keep the bot running when you close the terminal:
   ```bash
   caffeinate -i python -m src.bot  # Prevents Mac sleep
   ```

## Getting Your Credentials

| Credential | Where to get it | Looks like |
|------------|----------------|------------|
| Telegram Bot Token | Message [@BotFather](https://t.me/botfather), send `/newbot` | `123456789:ABCdef...` |
| Telegram User ID | Message [@userinfobot](https://t.me/userinfobot) | `123456789` |
| Anthropic API Key | [console.anthropic.com](https://console.anthropic.com) → API Keys | `sk-ant-...` |
| Groq API Key | [console.groq.com](https://console.groq.com) → API Keys (free) | `gsk_...` |
| OpenAI Key (optional) | [platform.openai.com/api-keys](https://platform.openai.com/api-keys) | `sk-proj-...` |

## Transcription Options

### Long Podcasts (45+ minutes)

Podcasts over 45 minutes are automatically split into 20-minute chunks and transcribed sequentially. Progress is shown in Telegram (`Transcribing chunk 2/4...`). If a chunk hits a transient error (rate limit, timeout), the bot retries once after 30 seconds before failing.

### Groq (Recommended)
Free tier, very fast. The bot auto-compresses audio to fit Groq's 25MB limit.

Groq's free tier has an hourly audio limit (~2 hours of audio per hour). If you also set `OPENAI_WHISPER_KEY`, the bot **automatically falls back to OpenAI** when Groq is rate-limited or the file is too large — no action needed from you.

**Railway env vars:**
```
GROQ_API_KEY=gsk_...
OPENAI_WHISPER_KEY=sk-proj-...   # optional fallback
```

**Local config.yaml:**
```yaml
whisper:
  mode: "cloud"
  groq_api_key: "gsk_..."
```

### OpenAI Only
Paid (~$0.006/min), no file size or rate limit issues. Use this if you don't want a Groq account.

**Railway env vars:**
```
OPENAI_WHISPER_KEY=sk-proj-...
```

**Local config.yaml:**
```yaml
whisper:
  mode: "cloud"
  openai_api_key: "sk-proj-..."
```

### Local (faster-whisper)
Free, private, requires decent CPU/GPU. Not available on Railway.

```yaml
whisper:
  mode: "local"
  model_size: "base"  # tiny, base, small, medium, large-v3
```

## Commands

| Command | Description |
|---------|-------------|
| `/podcast <url>` | Process a podcast episode |
| `/lookup` | Browse folders, search, and manage saved summaries |
| `/organize` | AI-powered folder reorganization (or batch-categorize existing) |
| `/status` | Check processing queue |
| `/stop` | Cancel stuck processes |
| `/stats` | View learning statistics |
| `/help` | Show available commands |
| `/poweron` / `/poweroff` | Start/stop bot (supervisor mode) |

### Supported Sources
- Spotify episode/show links
- Apple Podcasts links
- RSS feed URLs
- Direct audio URLs (MP3, M4A)

## Smart Folder System

Podcasts are automatically organized into a hierarchical folder structure powered by Claude AI.

### How It Works

1. **Save a podcast** → Claude analyzes the content and files it into the best folder
2. **Folders grow organically** → New folders are created when existing ones don't fit
3. **Auto-reorganization** → Every 5 saves, Claude reviews and cleans up the structure (merging duplicates, splitting overgrown folders)
4. **User control** → Create, rename, move, and delete folders via buttons in `/lookup`

### Browsing (`/lookup`)

The `/lookup` command shows your full library:

```
📚 Your Podcast Library

🤖 AI & Machine Learning (6)
💼 Business & Strategy (4)
🧠 Science & Health (2)

📋 Recent:
  1. Episode Title — Show Name (Jan 20)
  2. Episode Title — Show Name (Jan 18)

Reply with:
• A number to view a recent summary
• A folder name to browse that folder
• A question to search your library
```

- **Type a number** → View that summary
- **Type a folder name** → Browse folder contents (paginated, with sub-folders)
- **Type a question** → Smart search (substring match first, then AI semantic search)

### Folder Management

Inside any folder view, you get buttons for:
- **New Sub-folder** — Create a child folder
- **Rename** — Change folder name/emoji
- **Move** — Re-parent under a different folder
- **Delete** — Remove folder (contents move to parent)

On individual summaries:
- **Move to Folder** — Pick a destination folder

### `/organize` Command

- **First time** (no folders exist): Batch-categorizes all your existing podcasts into AI-generated folders
- **After that**: Triggers an AI cleanup pass — merges similar folders, splits large ones, renames unclear ones

## Troubleshooting

### "Bot doesn't respond"
- Verify your Telegram user ID is in `allowed_users` (or `TELEGRAM_ALLOWED_USERS` env var)
- Check the bot token is correct
- On Railway: check deployment logs for errors

### "Groq transcription failed"
- **File too large / Rate limited**: If you set `OPENAI_WHISPER_KEY`, the bot automatically falls back to OpenAI. Otherwise, wait for the rate limit to reset (~20 min) or try a shorter podcast.
- **Long podcast stuck on a chunk**: The bot retries once after 30s on transient errors. If it still fails, the error message will say which chunk failed — try again or set `OPENAI_WHISPER_KEY` for a more reliable fallback.
- **Trailing whitespace in key**: The bot auto-strips whitespace, but double-check your env var has no extra characters.

### "Can't find audio for Spotify podcast"
- Some Spotify podcasts are DRM-protected
- Try the podcast's RSS feed URL instead (search on podcasts.apple.com)
- If the wrong episode is matched, try a more specific Spotify episode link (not show link)

### "Telegram parse error in error messages"
- This is a known edge case with special characters in error text. The bot sanitizes output but some edge cases slip through. File an issue if you see this.

### "Bot is stuck / not responding to commands"
- Use `/stop` to clear all stuck sessions
- The ConversationHandler has a 10-minute timeout to auto-recover from stuck states

## Project Structure

```
knowledge-bot/
├── src/
│   ├── bot.py                # Main Telegram bot
│   ├── config.py             # Configuration (Pydantic models)
│   ├── processors/
│   │   └── podcast.py        # Audio download & transcription
│   ├── ai/
│   │   ├── summarizer.py     # Claude AI summaries
│   │   └── learning.py       # Preference learning
│   └── storage/
│       ├── summaries.py      # JSON-based summary persistence
│       └── categories.py     # Hierarchical folder/category system
├── Dockerfile                # Cloud deployment (Railway)
├── railway.toml              # Railway config
├── requirements.txt          # Full local dependencies
├── requirements-cloud.txt    # Cloud-only dependencies (no PyTorch)
├── config.yaml.example       # Configuration template (YAML)
├── .env.example              # Configuration template (env vars)
└── README.md
```

## Security

- **Access control**: Only whitelisted Telegram user IDs can use the bot
- **Secrets**: All API keys in `config.yaml` (gitignored) or environment variables
- **SSRF protection**: URL validation before fetching audio
- **Error sanitization**: No internal paths/stack traces in user-facing messages
- Never commit `config.yaml` or `.env` files

## License

MIT
