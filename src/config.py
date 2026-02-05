"""Configuration management for Knowledge Bot."""

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field


class TelegramConfig(BaseModel):
    """Telegram bot configuration."""

    bot_token: str
    # List of Telegram user IDs allowed to use the bot
    # Get your user ID by messaging @userinfobot on Telegram
    allowed_users: list[int] = Field(default_factory=list)


class ObsidianConfig(BaseModel):
    """Obsidian vault configuration."""

    vault_path: Path


class AIConfig(BaseModel):
    """AI/Claude configuration."""

    anthropic_api_key: str
    model: str = "claude-sonnet-4-20250514"


class WhisperConfig(BaseModel):
    """Whisper transcription configuration."""

    # Mode: "local" for faster-whisper, "cloud" for OpenAI/Groq Whisper API
    mode: Literal["local", "cloud"] = "local"

    # Local mode settings
    model_size: Literal["tiny", "base", "small", "medium", "large-v3"] = "base"
    device: str = "auto"

    # Cloud mode settings
    openai_api_key: str = ""
    groq_api_key: str = ""  # If set, uses Groq (faster + cheaper) instead of OpenAI


class DigestConfig(BaseModel):
    """Daily digest configuration."""

    time: str = "20:00"
    timezone: str = "America/Los_Angeles"


class SpotifyConfig(BaseModel):
    """Optional Spotify configuration."""

    client_id: str = ""
    client_secret: str = ""


class EmailConfig(BaseModel):
    """Optional email configuration for sending summaries.

    Supports two providers:
    - resend: Easy setup, just need API key (recommended)
    - smtp: Traditional SMTP (Gmail, etc.)
    """

    enabled: bool = False
    provider: Literal["resend", "smtp"] = "resend"

    # Resend configuration (recommended - just need API key)
    resend_api_key: str = ""
    from_email: str = "Podcast Bot <onboarding@resend.dev>"  # Default works with Resend free tier

    # SMTP configuration (alternative)
    smtp_server: str = "smtp.gmail.com"
    smtp_port: int = 587
    sender_email: str = ""
    sender_password: str = ""


def _get_groq_key() -> str:
    """Get Groq API key from environment variables."""
    # Primary: dedicated GROQ_API_KEY env var
    val = os.environ.get("GROQ_API_KEY", "").strip()
    if val.startswith("gsk_"):
        return val
    # Backward compat: WHISPER_MODE=groq:gsk_... (legacy Railway format)
    whisper_mode = os.environ.get("WHISPER_MODE", "").strip()
    if whisper_mode.startswith("groq:"):
        key = whisper_mode[5:].strip()
        if key.startswith("gsk_"):
            return key
    # Backward compat: OPENAI_API_KEY starting with gsk_
    openai_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if openai_key.startswith("gsk_"):
        return openai_key
    return ""


class Config(BaseModel):
    """Main configuration container."""

    telegram: TelegramConfig
    obsidian: ObsidianConfig
    ai: AIConfig
    whisper: WhisperConfig = Field(default_factory=WhisperConfig)
    digest: DigestConfig = Field(default_factory=DigestConfig)
    spotify: SpotifyConfig = Field(default_factory=SpotifyConfig)
    email: EmailConfig = Field(default_factory=EmailConfig)

    @classmethod
    def load(cls, config_path: Path | str = "config.yaml") -> "Config":
        """Load configuration from YAML file or environment variables.

        If config.yaml exists, loads from file with env var overrides.
        If no config.yaml, builds config entirely from environment variables:
        - TELEGRAM_BOT_TOKEN (required)
        - TELEGRAM_ALLOWED_USERS (comma-separated user IDs)
        - ANTHROPIC_API_KEY (required)
        - AI_MODEL (default: claude-sonnet-4-20250514)
        - WHISPER_MODE (local/cloud, default: cloud)
        - OPENAI_API_KEY (required if whisper mode is cloud)
        - VAULT_PATH (default: /data/vault)
        - DIGEST_TIME (default: 20:00)
        - DIGEST_TIMEZONE (default: America/Los_Angeles)
        - RESEND_API_KEY (optional)
        - EMAIL_ENABLED (optional, true/false)
        """
        config_path = Path(config_path)

        if config_path.exists():
            with open(config_path) as f:
                data = yaml.safe_load(f)
        elif os.environ.get("TELEGRAM_BOT_TOKEN"):
            # Build config entirely from environment variables
            allowed_users = []
            if os.environ.get("TELEGRAM_ALLOWED_USERS"):
                allowed_users = [int(x.strip()) for x in os.environ["TELEGRAM_ALLOWED_USERS"].split(",")]

            data = {
                "telegram": {
                    "bot_token": os.environ["TELEGRAM_BOT_TOKEN"].strip(),
                    "allowed_users": allowed_users,
                },
                "obsidian": {
                    "vault_path": os.environ.get("VAULT_PATH", "./data"),
                },
                "ai": {
                    "anthropic_api_key": os.environ["ANTHROPIC_API_KEY"].strip(),
                    "model": os.environ.get("AI_MODEL", "claude-sonnet-4-20250514"),
                },
                "whisper": {
                    "mode": "cloud" if os.environ.get("WHISPER_MODE", "cloud").startswith("groq:") else os.environ.get("WHISPER_MODE", "cloud"),
                    "openai_api_key": os.environ.get("OPENAI_API_KEY", "").strip(),
                    "groq_api_key": _get_groq_key(),
                },
                "digest": {
                    "time": os.environ.get("DIGEST_TIME", "20:00"),
                    "timezone": os.environ.get("DIGEST_TIMEZONE", "America/Los_Angeles"),
                },
                "email": {
                    "enabled": os.environ.get("EMAIL_ENABLED", "false").lower() == "true",
                    "resend_api_key": os.environ.get("RESEND_API_KEY", ""),
                },
            }
        else:
            raise FileNotFoundError(
                f"Configuration file not found: {config_path}\n"
                "Either create config.yaml or set TELEGRAM_BOT_TOKEN environment variable."
            )

        # Override secrets from environment variables if present (strip whitespace)
        if os.environ.get("TELEGRAM_BOT_TOKEN"):
            data["telegram"]["bot_token"] = os.environ["TELEGRAM_BOT_TOKEN"].strip()
        if os.environ.get("ANTHROPIC_API_KEY"):
            data["ai"]["anthropic_api_key"] = os.environ["ANTHROPIC_API_KEY"].strip()
        if os.environ.get("OPENAI_API_KEY"):
            data.setdefault("whisper", {})["openai_api_key"] = os.environ["OPENAI_API_KEY"].strip()
        if os.environ.get("GROQ_API_KEY"):
            data.setdefault("whisper", {})["groq_api_key"] = os.environ["GROQ_API_KEY"].strip()

        return cls.model_validate(data)


# Global config instance - initialized when bot starts
_config: Config | None = None


def get_config() -> Config:
    """Get the global configuration instance."""
    if _config is None:
        raise RuntimeError("Configuration not initialized. Call init_config() first.")
    return _config


def init_config(config_path: Path | str = "config.yaml") -> Config:
    """Initialize the global configuration."""
    global _config
    _config = Config.load(config_path)
    return _config
