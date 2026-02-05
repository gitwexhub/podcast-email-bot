"""Vector storage for semantic search and connections."""

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np


@dataclass
class ContentVector:
    """A content item with its embedding vector."""

    id: str
    content_type: str  # podcast, article, thread, note, insight
    title: str
    vault_path: str
    summary: str
    embedding: np.ndarray
    created_at: datetime


class VectorStore:
    """SQLite-based vector storage with numpy embeddings."""

    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        """Initialize the SQLite database."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS content_vectors (
                id TEXT PRIMARY KEY,
                content_type TEXT NOT NULL,
                title TEXT NOT NULL,
                vault_path TEXT NOT NULL,
                summary TEXT NOT NULL,
                embedding BLOB NOT NULL,
                created_at TEXT NOT NULL
            )
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_content_type ON content_vectors(content_type)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_created_at ON content_vectors(created_at)
        """)

        conn.commit()
        conn.close()

    def add(
        self,
        content_id: str,
        content_type: str,
        title: str,
        vault_path: str,
        summary: str,
        embedding: np.ndarray,
    ) -> None:
        """Add a content item with its embedding."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        # Convert embedding to bytes
        embedding_bytes = embedding.astype(np.float32).tobytes()

        cursor.execute(
            """
            INSERT OR REPLACE INTO content_vectors
            (id, content_type, title, vault_path, summary, embedding, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                content_id,
                content_type,
                title,
                vault_path,
                summary,
                embedding_bytes,
                datetime.now().isoformat(),
            ),
        )

        conn.commit()
        conn.close()

    def get_all_embeddings(self) -> list[tuple[str, np.ndarray]]:
        """Get all content IDs and their embeddings."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cursor.execute("SELECT id, embedding FROM content_vectors")
        results = []
        for row in cursor.fetchall():
            content_id = row[0]
            embedding = np.frombuffer(row[1], dtype=np.float32)
            results.append((content_id, embedding))

        conn.close()
        return results

    def get_by_id(self, content_id: str) -> Optional[ContentVector]:
        """Get a content item by ID."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cursor.execute(
            """
            SELECT id, content_type, title, vault_path, summary, embedding, created_at
            FROM content_vectors WHERE id = ?
            """,
            (content_id,),
        )
        row = cursor.fetchone()
        conn.close()

        if row:
            return ContentVector(
                id=row[0],
                content_type=row[1],
                title=row[2],
                vault_path=row[3],
                summary=row[4],
                embedding=np.frombuffer(row[5], dtype=np.float32),
                created_at=datetime.fromisoformat(row[6]),
            )
        return None

    def find_similar(
        self,
        query_embedding: np.ndarray,
        top_k: int = 5,
        exclude_id: str | None = None,
        content_type: str | None = None,
    ) -> list[tuple[ContentVector, float]]:
        """Find the most similar content items using cosine similarity.

        Returns list of (ContentVector, similarity_score) tuples.
        """
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        # Build query
        query = """
            SELECT id, content_type, title, vault_path, summary, embedding, created_at
            FROM content_vectors
        """
        params = []

        conditions = []
        if exclude_id:
            conditions.append("id != ?")
            params.append(exclude_id)
        if content_type:
            conditions.append("content_type = ?")
            params.append(content_type)

        if conditions:
            query += " WHERE " + " AND ".join(conditions)

        cursor.execute(query, params)
        rows = cursor.fetchall()
        conn.close()

        # Calculate similarities
        query_norm = query_embedding / np.linalg.norm(query_embedding)
        results = []

        for row in rows:
            embedding = np.frombuffer(row[5], dtype=np.float32)
            embedding_norm = embedding / np.linalg.norm(embedding)
            similarity = float(np.dot(query_norm, embedding_norm))

            content = ContentVector(
                id=row[0],
                content_type=row[1],
                title=row[2],
                vault_path=row[3],
                summary=row[4],
                embedding=embedding,
                created_at=datetime.fromisoformat(row[6]),
            )
            results.append((content, similarity))

        # Sort by similarity and return top_k
        results.sort(key=lambda x: x[1], reverse=True)
        return results[:top_k]

    def get_recent(self, days: int = 7, content_type: str | None = None) -> list[ContentVector]:
        """Get recently added content."""
        from datetime import timedelta

        cutoff = (datetime.now() - timedelta(days=days)).isoformat()

        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        query = "SELECT id, content_type, title, vault_path, summary, embedding, created_at FROM content_vectors WHERE created_at > ?"
        params = [cutoff]

        if content_type:
            query += " AND content_type = ?"
            params.append(content_type)

        query += " ORDER BY created_at DESC"

        cursor.execute(query, params)
        results = []
        for row in cursor.fetchall():
            results.append(
                ContentVector(
                    id=row[0],
                    content_type=row[1],
                    title=row[2],
                    vault_path=row[3],
                    summary=row[4],
                    embedding=np.frombuffer(row[5], dtype=np.float32),
                    created_at=datetime.fromisoformat(row[6]),
                )
            )

        conn.close()
        return results

    def delete(self, content_id: str) -> bool:
        """Delete a content item."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM content_vectors WHERE id = ?", (content_id,))
        deleted = cursor.rowcount > 0
        conn.commit()
        conn.close()
        return deleted

    def count(self) -> int:
        """Get total count of stored items."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM content_vectors")
        count = cursor.fetchone()[0]
        conn.close()
        return count
