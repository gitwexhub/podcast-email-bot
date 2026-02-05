"""Article extraction and processing."""

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional
from urllib.parse import urlparse

import httpx
import trafilatura

from src.config import Config
from src.storage.vault import ArticleMetadata, VaultWriter

logger = logging.getLogger(__name__)


@dataclass
class ArticleResult:
    """Result of article processing."""

    title: str
    author: Optional[str]
    summary: str
    key_points: list[str]
    vault_path: str
    url: str


class ArticleProcessor:
    """Handles article extraction and processing."""

    def __init__(self, config: Config, vault: VaultWriter):
        self.config = config
        self.vault = vault
        self._summarizer = None
        self._embedder = None

    def _get_summarizer(self):
        """Lazy load Claude summarizer."""
        if self._summarizer is None:
            from src.ai.summarizer import Summarizer
            self._summarizer = Summarizer(self.config)
        return self._summarizer

    def _get_embedder(self):
        """Lazy load embedder."""
        if self._embedder is None:
            from src.ai.embeddings import Embedder
            self._embedder = Embedder()
        return self._embedder

    async def process(self, url: str) -> ArticleResult:
        """Process an article URL end-to-end."""
        item_id = hashlib.md5(url.encode()).hexdigest()[:8]

        # Step 1: Fetch and extract article
        content, metadata = await self._extract_article(url)

        if not content:
            raise ValueError(f"Could not extract content from {url}")

        # Step 2: Generate summary
        summarizer = self._get_summarizer()
        summary_result = await summarizer.summarize_article(
            content=content,
            title=metadata.title,
            author=metadata.author,
            url=url,
        )

        # Step 3: Generate embedding and find connections
        embedder = self._get_embedder()
        embedding = embedder.embed(summary_result["summary"])

        from src.storage.vectors import VectorStore
        vector_store = VectorStore(self.config.obsidian.vault_path / ".vectors.db")
        similar = vector_store.find_similar(embedding, top_k=5, exclude_id=item_id)

        connections = []
        for cv, score in similar:
            if score > 0.5:
                connections.append(f"[[{cv.vault_path}|{cv.title}]] (similarity: {score:.2f})")

        # Step 4: Save to vault
        vault_path = self.vault.save_article(
            metadata=metadata,
            content=content,
            summary=summary_result["summary"],
            key_points=summary_result.get("key_points", []),
            connections=connections,
            tags=summary_result.get("tags", []),
        )

        # Step 5: Store embedding
        vector_store.add(
            content_id=item_id,
            content_type="article",
            title=metadata.title,
            vault_path=vault_path,
            summary=summary_result["summary"],
            embedding=embedding,
        )

        # Add summary to scratchpad
        self.vault.save_content_to_scratchpad(
            content_type="article",
            title=metadata.title,
            summary=summary_result["summary"],
            vault_path=vault_path,
        )

        return ArticleResult(
            title=metadata.title,
            author=metadata.author,
            summary=summary_result["summary"],
            key_points=summary_result.get("key_points", []),
            vault_path=vault_path,
            url=url,
        )

    async def _extract_article(self, url: str) -> tuple[str, ArticleMetadata]:
        """Extract article content and metadata from URL."""
        logger.info(f"Extracting article from: {url}")

        # Fetch the page
        async with httpx.AsyncClient() as client:
            response = await client.get(
                url,
                follow_redirects=True,
                headers={
                    "User-Agent": "Mozilla/5.0 (compatible; KnowledgeBot/1.0)"
                },
            )
            response.raise_for_status()
            html = response.text

        # Extract with trafilatura
        downloaded = trafilatura.extract(
            html,
            include_comments=False,
            include_tables=True,
            include_images=False,
            include_links=False,
            output_format="markdown",
            with_metadata=True,
            url=url,
        )

        if not downloaded:
            # Fallback: try without markdown formatting
            downloaded = trafilatura.extract(
                html,
                include_comments=False,
                output_format="txt",
                with_metadata=True,
                url=url,
            )

        if not downloaded:
            raise ValueError("Failed to extract article content")

        # Extract metadata
        metadata_dict = trafilatura.extract_metadata(html, url=url)

        title = "Untitled"
        author = None
        date = None
        site_name = None

        if metadata_dict:
            title = metadata_dict.title or title
            author = metadata_dict.author
            date = str(metadata_dict.date) if metadata_dict.date else None
            site_name = metadata_dict.sitename

        # If no title found, try to extract from URL
        if title == "Untitled":
            parsed = urlparse(url)
            path_parts = parsed.path.strip("/").split("/")
            if path_parts:
                title = path_parts[-1].replace("-", " ").replace("_", " ").title()

        metadata = ArticleMetadata(
            title=title,
            author=author,
            date=date or datetime.now().strftime("%Y-%m-%d"),
            url=url,
            site_name=site_name or urlparse(url).netloc,
        )

        return downloaded, metadata

    async def extract_content_only(self, url: str) -> str:
        """Extract just the article content without processing.

        Useful for previewing or when summary is not needed.
        """
        async with httpx.AsyncClient() as client:
            response = await client.get(
                url,
                follow_redirects=True,
                headers={
                    "User-Agent": "Mozilla/5.0 (compatible; KnowledgeBot/1.0)"
                },
            )
            response.raise_for_status()
            html = response.text

        content = trafilatura.extract(
            html,
            include_comments=False,
            output_format="txt",
        )

        return content or ""
