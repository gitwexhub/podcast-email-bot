"""X/Twitter thread scraping and processing."""

import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Optional
from urllib.parse import urlparse

import httpx

from src.config import Config
from src.storage.vault import ThreadMetadata, VaultWriter

logger = logging.getLogger(__name__)


@dataclass
class ThreadResult:
    """Result of thread processing."""

    author: str
    tweet_count: int
    summary: str
    vault_path: str
    url: str


class ThreadProcessor:
    """Handles X/Twitter thread scraping and processing."""

    # List of Nitter instances (some may be down, will try in order)
    NITTER_INSTANCES = [
        "nitter.privacydev.net",
        "nitter.poast.org",
        "nitter.cz",
        "nitter.1d4.us",
    ]

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

    async def process(self, url: str) -> ThreadResult:
        """Process an X/Twitter thread URL end-to-end."""
        item_id = hashlib.md5(url.encode()).hexdigest()[:8]

        # Step 1: Extract thread
        tweets, metadata = await self._extract_thread(url)

        if not tweets:
            raise ValueError(f"Could not extract thread from {url}")

        # Step 2: Generate summary
        summarizer = self._get_summarizer()
        summary_result = await summarizer.summarize_thread(
            tweets=tweets,
            author=metadata.author,
        )

        # Step 3: Generate embedding and find connections
        full_text = " ".join(tweets)
        embedder = self._get_embedder()
        embedding = embedder.embed(summary_result["summary"] + " " + full_text[:2000])

        from src.storage.vectors import VectorStore
        vector_store = VectorStore(self.config.obsidian.vault_path / ".vectors.db")
        similar = vector_store.find_similar(embedding, top_k=5, exclude_id=item_id)

        connections = []
        for cv, score in similar:
            if score > 0.5:
                connections.append(f"[[{cv.vault_path}|{cv.title}]] (similarity: {score:.2f})")

        # Step 4: Save to vault
        vault_path = self.vault.save_thread(
            metadata=metadata,
            tweets=tweets,
            summary=summary_result["summary"],
            connections=connections,
            tags=summary_result.get("tags", []),
        )

        # Step 5: Store embedding
        vector_store.add(
            content_id=item_id,
            content_type="thread",
            title=f"Thread by @{metadata.author}",
            vault_path=vault_path,
            summary=summary_result["summary"],
            embedding=embedding,
        )

        # Add summary to scratchpad
        self.vault.save_content_to_scratchpad(
            content_type="thread",
            title=f"Thread by @{metadata.author}",
            summary=summary_result["summary"],
            vault_path=vault_path,
        )

        return ThreadResult(
            author=metadata.author,
            tweet_count=metadata.tweet_count,
            summary=summary_result["summary"],
            vault_path=vault_path,
            url=url,
        )

    def _convert_to_nitter_url(self, url: str) -> tuple[str, str]:
        """Convert X/Twitter URL to Nitter URL.

        Returns (nitter_url, username).
        """
        # Parse the original URL
        parsed = urlparse(url)

        # Extract path components
        # URLs look like: twitter.com/user/status/123456 or x.com/user/status/123456
        path_parts = parsed.path.strip("/").split("/")

        if len(path_parts) < 3:
            raise ValueError(f"Invalid thread URL format: {url}")

        username = path_parts[0]
        status_id = path_parts[2]

        # Try Nitter instances
        for instance in self.NITTER_INSTANCES:
            nitter_url = f"https://{instance}/{username}/status/{status_id}"
            return nitter_url, username

        raise ValueError("No Nitter instances available")

    async def _extract_thread(self, url: str) -> tuple[list[str], ThreadMetadata]:
        """Extract thread content using Nitter or direct parsing."""
        # First try Nitter (more reliable for full threads)
        try:
            return await self._extract_via_nitter(url)
        except Exception as e:
            logger.warning(f"Nitter extraction failed: {e}, trying direct method")

        # Fallback to direct extraction
        return await self._extract_direct(url)

    async def _extract_via_nitter(self, url: str) -> tuple[list[str], ThreadMetadata]:
        """Extract thread content via a Nitter instance."""
        # Parse original URL for username
        parsed = urlparse(url)
        path_parts = parsed.path.strip("/").split("/")
        if len(path_parts) < 3:
            raise ValueError(f"Invalid URL format: {url}")

        username = path_parts[0]
        status_id = path_parts[2]

        tweets = []
        last_error = None

        async with httpx.AsyncClient(timeout=30.0) as client:
            for instance in self.NITTER_INSTANCES:
                try:
                    nitter_url = f"https://{instance}/{username}/status/{status_id}"
                    logger.info(f"Trying Nitter instance: {instance}")

                    response = await client.get(
                        nitter_url,
                        follow_redirects=True,
                        headers={
                            "User-Agent": "Mozilla/5.0 (compatible; KnowledgeBot/1.0)"
                        },
                    )

                    if response.status_code == 200:
                        tweets = self._parse_nitter_html(response.text)
                        if tweets:
                            break

                except Exception as e:
                    last_error = e
                    logger.debug(f"Instance {instance} failed: {e}")
                    continue

        if not tweets:
            if last_error:
                raise last_error
            raise ValueError("Could not extract thread from any Nitter instance")

        metadata = ThreadMetadata(
            author=username,
            date=datetime.now().strftime("%Y-%m-%d"),
            url=url,
            tweet_count=len(tweets),
        )

        return tweets, metadata

    def _parse_nitter_html(self, html: str) -> list[str]:
        """Parse tweets from Nitter HTML."""
        tweets = []

        # Find tweet content divs
        # Nitter uses class="tweet-content media-body"
        content_pattern = r'<div class="tweet-content[^"]*"[^>]*>(.*?)</div>'
        matches = re.findall(content_pattern, html, re.DOTALL)

        for match in matches:
            # Clean up HTML
            text = self._clean_html(match)
            if text and len(text) > 5:
                tweets.append(text)

        return tweets

    def _clean_html(self, html: str) -> str:
        """Remove HTML tags and clean up text."""
        # Remove HTML tags
        text = re.sub(r"<[^>]+>", "", html)

        # Decode HTML entities
        import html as html_lib
        text = html_lib.unescape(text)

        # Clean up whitespace
        text = re.sub(r"\s+", " ", text).strip()

        return text

    async def _extract_direct(self, url: str) -> tuple[list[str], ThreadMetadata]:
        """Direct extraction fallback (limited, single tweet only)."""
        # Parse URL
        parsed = urlparse(url)
        path_parts = parsed.path.strip("/").split("/")

        if len(path_parts) < 3:
            raise ValueError(f"Invalid URL format: {url}")

        username = path_parts[0]

        # Try to fetch with oembed API (gives at least the main tweet)
        async with httpx.AsyncClient() as client:
            oembed_url = f"https://publish.twitter.com/oembed?url={url}"

            try:
                response = await client.get(oembed_url)
                if response.status_code == 200:
                    data = response.json()
                    html = data.get("html", "")
                    # Extract text from the oembed HTML
                    text = self._clean_html(html)
                    if text:
                        # This will only get the first tweet, but better than nothing
                        tweets = [text]

                        metadata = ThreadMetadata(
                            author=username,
                            date=datetime.now().strftime("%Y-%m-%d"),
                            url=url,
                            tweet_count=1,
                        )

                        return tweets, metadata
            except Exception as e:
                logger.warning(f"oembed fallback failed: {e}")

        raise ValueError(
            "Could not extract thread. Nitter instances may be unavailable. "
            "Try providing the thread content directly using /note."
        )

    async def extract_thread_preview(self, url: str) -> Optional[str]:
        """Get a quick preview of a thread without full processing."""
        try:
            tweets, metadata = await self._extract_thread(url)
            if tweets:
                preview = f"Thread by @{metadata.author} ({len(tweets)} tweets):\n\n"
                preview += tweets[0][:200] + "..."
                return preview
        except Exception:
            pass
        return None
