"""Tests for vector storage functionality."""

import tempfile
from pathlib import Path

import numpy as np
import pytest

from src.storage.vectors import VectorStore


@pytest.fixture
def temp_db():
    """Create a temporary database file."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir) / "test.db"


@pytest.fixture
def vector_store(temp_db):
    """Create a VectorStore instance."""
    return VectorStore(temp_db)


class TestVectorStore:
    """Tests for VectorStore."""

    def test_add_and_retrieve(self, vector_store):
        """Test adding and retrieving content."""
        embedding = np.random.randn(384).astype(np.float32)

        vector_store.add(
            content_id="test-1",
            content_type="article",
            title="Test Article",
            vault_path="content/articles/test.md",
            summary="This is a test summary",
            embedding=embedding,
        )

        result = vector_store.get_by_id("test-1")
        assert result is not None
        assert result.title == "Test Article"
        assert result.content_type == "article"
        assert np.allclose(result.embedding, embedding)

    def test_find_similar(self, vector_store):
        """Test finding similar content."""
        # Add some content with different embeddings
        base_embedding = np.random.randn(384).astype(np.float32)
        base_embedding = base_embedding / np.linalg.norm(base_embedding)

        # Add similar item
        similar = base_embedding + np.random.randn(384).astype(np.float32) * 0.1
        similar = similar / np.linalg.norm(similar)

        # Add dissimilar item
        dissimilar = np.random.randn(384).astype(np.float32)
        dissimilar = dissimilar / np.linalg.norm(dissimilar)

        vector_store.add("id-1", "article", "Similar Article", "path1.md", "summary1", similar)
        vector_store.add("id-2", "article", "Different Article", "path2.md", "summary2", dissimilar)

        # Find similar to base
        results = vector_store.find_similar(base_embedding, top_k=2)

        assert len(results) == 2
        # The similar one should be first (higher similarity)
        assert results[0][0].title == "Similar Article"
        assert results[0][1] > results[1][1]  # Higher similarity score

    def test_count(self, vector_store):
        """Test counting items."""
        assert vector_store.count() == 0

        embedding = np.random.randn(384).astype(np.float32)
        vector_store.add("id-1", "article", "Article 1", "path1.md", "summary", embedding)
        assert vector_store.count() == 1

        vector_store.add("id-2", "podcast", "Podcast 1", "path2.md", "summary", embedding)
        assert vector_store.count() == 2

    def test_delete(self, vector_store):
        """Test deleting items."""
        embedding = np.random.randn(384).astype(np.float32)
        vector_store.add("id-1", "article", "Article 1", "path1.md", "summary", embedding)

        assert vector_store.count() == 1
        assert vector_store.delete("id-1") is True
        assert vector_store.count() == 0
        assert vector_store.get_by_id("id-1") is None

    def test_upsert(self, vector_store):
        """Test that adding same ID updates the record."""
        embedding1 = np.random.randn(384).astype(np.float32)
        embedding2 = np.random.randn(384).astype(np.float32)

        vector_store.add("id-1", "article", "Original Title", "path1.md", "summary", embedding1)
        vector_store.add("id-1", "article", "Updated Title", "path1.md", "summary", embedding2)

        assert vector_store.count() == 1
        result = vector_store.get_by_id("id-1")
        assert result.title == "Updated Title"
