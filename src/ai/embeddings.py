"""Embedding generation for semantic search."""

import logging
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)


class Embedder:
    """Generate embeddings using sentence-transformers."""

    def __init__(self, model_name: str = "all-MiniLM-L6-v2"):
        """Initialize the embedder with a specific model.

        Args:
            model_name: Name of the sentence-transformers model to use.
                        Default is 'all-MiniLM-L6-v2' which is fast and effective.
                        For better quality, use 'all-mpnet-base-v2'.
        """
        self.model_name = model_name
        self._model = None

    def _get_model(self):
        """Lazy load the embedding model."""
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            logger.info(f"Loading embedding model: {self.model_name}")
            self._model = SentenceTransformer(self.model_name)
        return self._model

    def embed(self, text: str) -> np.ndarray:
        """Generate embedding for a single text.

        Args:
            text: Text to embed

        Returns:
            numpy array of shape (embedding_dim,)
        """
        model = self._get_model()

        # Truncate very long texts (model has max sequence length)
        max_chars = 10000
        if len(text) > max_chars:
            text = text[:max_chars]

        embedding = model.encode(text, convert_to_numpy=True)
        return embedding

    def embed_batch(self, texts: list[str]) -> np.ndarray:
        """Generate embeddings for multiple texts.

        Args:
            texts: List of texts to embed

        Returns:
            numpy array of shape (num_texts, embedding_dim)
        """
        model = self._get_model()

        # Truncate long texts
        max_chars = 10000
        texts = [t[:max_chars] if len(t) > max_chars else t for t in texts]

        embeddings = model.encode(texts, convert_to_numpy=True, show_progress_bar=True)
        return embeddings

    def similarity(self, embedding1: np.ndarray, embedding2: np.ndarray) -> float:
        """Calculate cosine similarity between two embeddings."""
        norm1 = np.linalg.norm(embedding1)
        norm2 = np.linalg.norm(embedding2)

        if norm1 == 0 or norm2 == 0:
            return 0.0

        return float(np.dot(embedding1, embedding2) / (norm1 * norm2))

    @property
    def embedding_dim(self) -> int:
        """Get the dimension of embeddings produced by this model."""
        model = self._get_model()
        return model.get_sentence_embedding_dimension()
