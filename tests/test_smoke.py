"""Smoke tests — fast, no external API calls, no network.

These verify the three things most likely to silently break before a deploy:
  1. Config loads without crashing
  2. Chunking logic doesn't create a tiny invalid last chunk
  3. Summary storage round-trips correctly
"""

import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# 1. Config loading
# ---------------------------------------------------------------------------

def test_config_loads_from_env_vars(monkeypatch):
    """Config.load() should succeed when required env vars are set."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:FAKE_TOKEN")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "12345")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_fake")

    from src.config import Config

    # Use a non-existent path so it falls back to env vars
    config = Config.load(config_path="/tmp/does_not_exist.yaml")

    assert config.telegram.bot_token == "123456:FAKE_TOKEN"
    assert config.telegram.allowed_users == [12345]
    assert config.ai.anthropic_api_key == "sk-ant-fake"


def test_config_strips_whitespace_from_keys(monkeypatch):
    """Keys with accidental trailing spaces should still load correctly."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "  123456:FAKE_TOKEN  ")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "12345")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "  sk-ant-fake  ")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_fake")

    from src.config import Config

    config = Config.load(config_path="/tmp/does_not_exist.yaml")
    assert config.telegram.bot_token == "123456:FAKE_TOKEN"
    assert config.ai.anthropic_api_key == "sk-ant-fake"


# ---------------------------------------------------------------------------
# 2. Chunking logic — the bug we fixed today
# ---------------------------------------------------------------------------

@pytest.fixture
def podcast_processor():
    """Create a PodcastProcessor with a fake config (no real API keys needed)."""
    from src.processors.podcast import PodcastProcessor

    config = MagicMock()
    config.whisper.groq_api_key = "gsk_fake"
    config.whisper.openai_api_key = ""
    vault = MagicMock()
    return PodcastProcessor(config, vault)


async def test_split_audio_skips_tiny_last_chunk(podcast_processor, tmp_path):
    """A podcast that's slightly over a chunk boundary should NOT produce a tiny last chunk.

    Regression test for: 400 'could not process file' on chunk 5/5 of an 80-min podcast
    where the last chunk was only ~30 seconds long.
    """
    fake_chunk = tmp_path / "audio_chunk004.mp3"
    fake_chunk.write_bytes(b"x" * 100)  # non-empty but tiny

    # Patch ffmpeg (split) to create chunks, with the last one being 25 seconds
    call_count = 0

    async def fake_get_duration(path):
        nonlocal call_count
        call_count += 1
        # First call: full podcast duration (80 min 25 sec → 5 chunks, last = 25s)
        if call_count == 1:
            return 4825.0
        # Subsequent calls: per-chunk duration check
        chunk_index = int(path.stem.split("chunk")[-1])
        return 25.0 if chunk_index == 4 else 1200.0

    def fake_subprocess_run(cmd, **kwargs):
        # Extract chunk path from ffmpeg command and create the file
        chunk_path = Path(cmd[-1])
        chunk_path.write_bytes(b"x" * 1000)
        return MagicMock(returncode=0)

    with patch.object(podcast_processor, "_get_audio_duration", side_effect=fake_get_duration), \
         patch("subprocess.run", side_effect=fake_subprocess_run):

        # Create a fake source audio file
        source = tmp_path / "podcast.mp3"
        source.write_bytes(b"x" * 1000)

        chunks = await podcast_processor._split_audio(source, chunk_duration=1200)

    # Should have 4 chunks, not 5 — the 25-second tail was dropped
    assert len(chunks) == 4, (
        f"Expected 4 chunks (tiny last chunk should be skipped), got {len(chunks)}"
    )


async def test_split_audio_keeps_chunk_meeting_minimum(podcast_processor, tmp_path):
    """Chunks of exactly 30 seconds should be kept (boundary case)."""
    call_count = 0

    async def fake_get_duration(path):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return 4830.0  # 80 min 30 sec → last chunk = exactly 30s
        chunk_index = int(path.stem.split("chunk")[-1])
        return 30.0 if chunk_index == 4 else 1200.0

    def fake_subprocess_run(cmd, **kwargs):
        Path(cmd[-1]).write_bytes(b"x" * 1000)
        return MagicMock(returncode=0)

    with patch.object(podcast_processor, "_get_audio_duration", side_effect=fake_get_duration), \
         patch("subprocess.run", side_effect=fake_subprocess_run):

        source = tmp_path / "podcast.mp3"
        source.write_bytes(b"x" * 1000)
        chunks = await podcast_processor._split_audio(source, chunk_duration=1200)

    # 30s is exactly the threshold — should be kept
    assert len(chunks) == 5


# ---------------------------------------------------------------------------
# 3. Summary storage round-trip
# ---------------------------------------------------------------------------

def test_summary_save_and_reload(tmp_path):
    """A saved summary should survive a full serialise → reload cycle."""
    from src.storage.summaries import SummaryStorage

    storage_path = tmp_path / "summaries.json"
    storage = SummaryStorage(storage_path)

    summary_id = storage.save_summary(
        title="Test Episode",
        email_content="## Summary\nGreat episode.",
        transcript="Full transcript here.",
        show="Test Podcast",
        url="https://example.com/ep1",
        duration="1:02:00",
    )

    # Reload from disk (simulates a bot restart)
    reloaded = SummaryStorage(storage_path)
    summary = reloaded.get_summary(summary_id)

    assert summary is not None
    assert summary.title == "Test Episode"
    assert summary.show == "Test Podcast"
    assert summary.email_content == "## Summary\nGreat episode."
    assert summary.categories == []  # default


def test_summary_categories_persist(tmp_path):
    """Categories assigned to a summary should survive a reload."""
    from src.storage.summaries import SummaryStorage

    storage_path = tmp_path / "summaries.json"
    storage = SummaryStorage(storage_path)

    summary_id = storage.save_summary(
        title="AI Episode",
        email_content="Summary text.",
        transcript="Transcript.",
    )
    storage.update_categories(summary_id, ["AI & Tech", "Startups"])

    reloaded = SummaryStorage(storage_path)
    summary = reloaded.get_summary(summary_id)

    assert summary.categories == ["AI & Tech", "Startups"]


def test_summary_missing_categories_field_backward_compat(tmp_path):
    """Old summaries without a 'categories' field should load cleanly."""
    import json
    from src.storage.summaries import SummaryStorage

    storage_path = tmp_path / "summaries.json"
    # Write an old-format summary without the categories field
    storage_path.write_text(json.dumps([{
        "id": "abc123",
        "title": "Old Episode",
        "show": "Old Show",
        "email_content": "Content",
        "transcript": "Transcript",
        "url": None,
        "duration": None,
        "created_at": "2024-01-01T00:00:00",
        "updated_at": "2024-01-01T00:00:00",
        # no 'categories' key — intentionally omitted
    }]))

    storage = SummaryStorage(storage_path)
    summary = storage.get_summary("abc123")

    assert summary is not None
    assert summary.categories == []
