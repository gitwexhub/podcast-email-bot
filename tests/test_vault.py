"""Tests for vault writer functionality."""

import tempfile
from datetime import datetime
from pathlib import Path

import pytest

from src.storage.vault import (
    ArticleMetadata,
    PodcastMetadata,
    Soundbite,
    ThreadMetadata,
    VaultWriter,
)


@pytest.fixture
def temp_vault():
    """Create a temporary vault directory."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def vault_writer(temp_vault):
    """Create a VaultWriter instance."""
    return VaultWriter(temp_vault)


class TestVaultWriter:
    """Tests for VaultWriter."""

    def test_creates_directory_structure(self, vault_writer, temp_vault):
        """Test that vault directories are created."""
        expected_dirs = [
            "daily",
            "content/podcasts",
            "content/articles",
            "content/threads",
            "notes",
            "insights",
            "templates",
        ]
        for dir_name in expected_dirs:
            assert (temp_vault / dir_name).exists()

    def test_save_note(self, vault_writer):
        """Test saving a quick note."""
        note_text = "This is a test note"
        result = vault_writer.save_note(note_text)

        assert result.startswith("notes/")
        assert result.endswith(".md")

        # Verify file contents
        full_path = vault_writer.vault_path / result
        assert full_path.exists()
        content = full_path.read_text()
        assert "type: note" in content
        assert note_text in content

    def test_save_insight(self, vault_writer):
        """Test saving an insight."""
        insight_text = "Key insight about something important"
        result = vault_writer.save_insight(insight_text)

        assert result.startswith("insights/")
        full_path = vault_writer.vault_path / result
        content = full_path.read_text()
        assert "type: insight" in content
        assert insight_text in content

    def test_save_podcast(self, vault_writer):
        """Test saving a podcast."""
        metadata = PodcastMetadata(
            title="Test Podcast Episode",
            show_name="Test Show",
            date="2024-01-15",
            duration=3600,
            url="https://example.com/podcast",
        )

        soundbites = [
            Soundbite(
                text="This is a memorable quote",
                timestamp="12:34",
                speaker="Host",
                context="Discussing important topic",
            )
        ]

        result = vault_writer.save_podcast(
            metadata=metadata,
            transcript="Full transcript text here...",
            summary="This is the summary.",
            key_points=["Point 1", "Point 2"],
            soundbites=soundbites,
            connections=["[[related-note]]"],
            tags=["technology", "interview"],
        )

        assert result.startswith("content/podcasts/")
        full_path = vault_writer.vault_path / result
        content = full_path.read_text()

        assert 'title: "Test Podcast Episode"' in content
        assert "type: podcast" in content
        assert "This is the summary." in content
        assert "This is a memorable quote" in content
        assert "technology" in content

    def test_save_article(self, vault_writer):
        """Test saving an article."""
        metadata = ArticleMetadata(
            title="Test Article Title",
            author="John Doe",
            date="2024-01-15",
            url="https://example.com/article",
            site_name="Example Site",
        )

        result = vault_writer.save_article(
            metadata=metadata,
            content="Full article content here...",
            summary="Article summary.",
            key_points=["Key point 1"],
            tags=["tech"],
        )

        assert result.startswith("content/articles/")
        full_path = vault_writer.vault_path / result
        content = full_path.read_text()

        assert 'title: "Test Article Title"' in content
        assert "type: article" in content
        assert "John Doe" in content

    def test_save_thread(self, vault_writer):
        """Test saving a thread."""
        metadata = ThreadMetadata(
            author="testuser",
            date="2024-01-15",
            url="https://x.com/testuser/status/123",
            tweet_count=5,
        )

        result = vault_writer.save_thread(
            metadata=metadata,
            tweets=["Tweet 1", "Tweet 2", "Tweet 3"],
            summary="Thread summary.",
        )

        assert result.startswith("content/threads/")
        full_path = vault_writer.vault_path / result
        content = full_path.read_text()

        assert 'author: "@testuser"' in content
        assert "type: thread" in content
        assert "Tweet 1" in content

    def test_sanitize_filename(self, vault_writer):
        """Test filename sanitization."""
        dirty_name = 'Test: "Podcast" with/bad\\chars?'
        clean = vault_writer._sanitize_filename(dirty_name)

        assert ":" not in clean
        assert '"' not in clean
        assert "/" not in clean
        assert "\\" not in clean
        assert "?" not in clean
        assert len(clean) <= 50
