"""Find and suggest connections between content items."""

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np

from src.ai.embeddings import Embedder
from src.ai.summarizer import Summarizer
from src.config import Config
from src.storage.vectors import ContentVector, VectorStore

logger = logging.getLogger(__name__)


@dataclass
class Connection:
    """A connection between two pieces of content."""

    source_id: str
    target_id: str
    target_title: str
    target_path: str
    similarity_score: float
    description: Optional[str] = None


class ConnectionFinder:
    """Finds semantic connections between content items."""

    def __init__(self, config: Config, vector_store: VectorStore):
        self.config = config
        self.vector_store = vector_store
        self._embedder = None
        self._summarizer = None

    def _get_embedder(self) -> Embedder:
        """Lazy load embedder."""
        if self._embedder is None:
            self._embedder = Embedder()
        return self._embedder

    def _get_summarizer(self) -> Summarizer:
        """Lazy load summarizer."""
        if self._summarizer is None:
            self._summarizer = Summarizer(self.config)
        return self._summarizer

    def find_connections(
        self,
        content_id: str,
        summary: str,
        embedding: Optional[np.ndarray] = None,
        top_k: int = 5,
        min_similarity: float = 0.4,
    ) -> list[Connection]:
        """Find connections to existing content.

        Args:
            content_id: ID of the new content
            summary: Summary text of the new content
            embedding: Pre-computed embedding (if available)
            top_k: Maximum number of connections to return
            min_similarity: Minimum similarity threshold

        Returns:
            List of Connection objects sorted by similarity
        """
        # Generate embedding if not provided
        if embedding is None:
            embedder = self._get_embedder()
            embedding = embedder.embed(summary)

        # Find similar items
        similar = self.vector_store.find_similar(
            query_embedding=embedding,
            top_k=top_k,
            exclude_id=content_id,
        )

        # Filter by minimum similarity
        connections = []
        for content_vector, score in similar:
            if score >= min_similarity:
                connections.append(
                    Connection(
                        source_id=content_id,
                        target_id=content_vector.id,
                        target_title=content_vector.title,
                        target_path=content_vector.vault_path,
                        similarity_score=score,
                    )
                )

        return connections

    async def find_connections_with_descriptions(
        self,
        content_id: str,
        summary: str,
        embedding: Optional[np.ndarray] = None,
        top_k: int = 3,
        min_similarity: float = 0.5,
    ) -> list[Connection]:
        """Find connections and generate natural language descriptions.

        This uses Claude to generate meaningful descriptions of how
        the content items relate to each other.
        """
        # First find basic connections
        connections = self.find_connections(
            content_id=content_id,
            summary=summary,
            embedding=embedding,
            top_k=top_k,
            min_similarity=min_similarity,
        )

        if not connections:
            return []

        # Get summaries for connected items
        similar_items = []
        for conn in connections:
            item = self.vector_store.get_by_id(conn.target_id)
            if item:
                similar_items.append(
                    {
                        "title": item.title,
                        "summary": item.summary,
                        "vault_path": item.vault_path,
                    }
                )

        # Generate descriptions via Claude
        summarizer = self._get_summarizer()
        descriptions = await summarizer.generate_connections(
            new_content_summary=summary,
            similar_items=similar_items,
        )

        # Attach descriptions to connections
        for i, conn in enumerate(connections):
            if i < len(descriptions):
                conn.description = descriptions[i]

        return connections

    def format_connections_for_vault(self, connections: list[Connection]) -> list[str]:
        """Format connections as markdown links for Obsidian vault."""
        formatted = []
        for conn in connections:
            link = f"[[{conn.target_path}|{conn.target_title}]]"
            if conn.description:
                formatted.append(f"{conn.description} - {link}")
            else:
                formatted.append(f"Related: {link} (similarity: {conn.similarity_score:.0%})")
        return formatted


class ConnectionGraphBuilder:
    """Build and analyze the connection graph."""

    def __init__(self, vector_store: VectorStore):
        self.vector_store = vector_store

    def get_most_connected(self, top_k: int = 10) -> list[tuple[ContentVector, int]]:
        """Find content items with the most connections above threshold."""
        all_embeddings = self.vector_store.get_all_embeddings()

        # Count connections for each item
        connection_counts = {}

        for content_id, embedding in all_embeddings:
            similar = self.vector_store.find_similar(
                query_embedding=embedding,
                top_k=20,
                exclude_id=content_id,
            )
            # Count items above threshold
            count = sum(1 for _, score in similar if score >= 0.5)
            connection_counts[content_id] = count

        # Sort by connection count
        sorted_items = sorted(
            connection_counts.items(),
            key=lambda x: x[1],
            reverse=True,
        )[:top_k]

        # Get full content vectors
        results = []
        for content_id, count in sorted_items:
            content = self.vector_store.get_by_id(content_id)
            if content:
                results.append((content, count))

        return results

    def get_clusters(self, threshold: float = 0.6) -> list[list[ContentVector]]:
        """Find clusters of highly related content."""
        all_items = self.vector_store.get_all_embeddings()

        if not all_items:
            return []

        # Simple clustering using connected components
        adjacency = {}
        for content_id, embedding in all_items:
            similar = self.vector_store.find_similar(
                query_embedding=embedding,
                top_k=10,
                exclude_id=content_id,
            )
            adjacency[content_id] = [
                cid for cv, score in similar
                for cid in [cv.id] if score >= threshold
            ]

        # Find connected components
        visited = set()
        clusters = []

        def dfs(node_id: str, cluster: list):
            if node_id in visited:
                return
            visited.add(node_id)
            content = self.vector_store.get_by_id(node_id)
            if content:
                cluster.append(content)
            for neighbor in adjacency.get(node_id, []):
                dfs(neighbor, cluster)

        for content_id, _ in all_items:
            if content_id not in visited:
                cluster = []
                dfs(content_id, cluster)
                if len(cluster) > 1:  # Only include clusters with multiple items
                    clusters.append(cluster)

        # Sort clusters by size
        clusters.sort(key=len, reverse=True)
        return clusters
