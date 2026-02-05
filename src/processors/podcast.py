"""Podcast processing: download, transcribe, summarize."""

import asyncio
import hashlib
import logging
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import feedparser
import httpx

from src.config import Config
from src.storage.vault import PodcastMetadata, Soundbite, VaultWriter

logger = logging.getLogger(__name__)


@dataclass
class TranscriptSegment:
    """A segment of transcribed audio with timing."""

    text: str
    start: float  # seconds
    end: float  # seconds

    @property
    def timestamp(self) -> str:
        """Format start time as MM:SS or HH:MM:SS."""
        hours, remainder = divmod(int(self.start), 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours:
            return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        return f"{minutes:02d}:{seconds:02d}"


@dataclass
class PodcastResult:
    """Result of podcast processing."""

    title: str
    show_name: Optional[str]
    duration: int  # seconds
    duration_str: str
    summary: str
    key_points: list[str]
    soundbites: list[Soundbite]
    vault_path: str
    transcript: str


@dataclass
class QueueItem:
    """An item in the processing queue."""

    id: str
    url: str
    title: str
    status: str = "queued"  # queued, downloading, transcribing, summarizing, complete, error
    progress: float = 0.0
    error: Optional[str] = None
    started_at: Optional[float] = None  # timestamp when processing started
    duration_seconds: Optional[int] = None  # podcast duration in seconds


class PodcastProcessor:
    """Handles podcast downloading, transcription, and processing."""

    def __init__(self, config: Config, vault: VaultWriter):
        self.config = config
        self.vault = vault
        self.queue: list[QueueItem] = []
        self._whisper_model = None
        self._summarizer = None
        self._embedder = None

    def _get_whisper_model(self):
        """Lazy load Whisper model."""
        if self._whisper_model is None:
            from faster_whisper import WhisperModel

            device = self.config.whisper.device
            if device == "auto":
                device = "cuda" if self._cuda_available() else "cpu"

            compute_type = "float16" if device == "cuda" else "int8"

            logger.info(
                f"Loading Whisper model: {self.config.whisper.model_size} on {device}"
            )
            self._whisper_model = WhisperModel(
                self.config.whisper.model_size,
                device=device,
                compute_type=compute_type,
            )
        return self._whisper_model

    def _cuda_available(self) -> bool:
        """Check if CUDA is available."""
        try:
            import torch
            return torch.cuda.is_available()
        except ImportError:
            return False

    def unload_whisper_model(self) -> None:
        """Unload Whisper model to free memory."""
        if self._whisper_model is not None:
            logger.info("Unloading Whisper model to free memory")
            del self._whisper_model
            self._whisper_model = None
            # Force garbage collection
            import gc
            gc.collect()

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

    async def process(self, url: str) -> PodcastResult:
        """Process a podcast URL end-to-end."""
        # Create queue item
        item_id = hashlib.md5(url.encode()).hexdigest()[:8]
        queue_item = QueueItem(id=item_id, url=url, title="Processing...")
        self.queue.append(queue_item)

        try:
            # Step 1: Resolve URL and get audio
            queue_item.status = "downloading"
            audio_path, metadata = await self._download_audio(url)
            queue_item.title = metadata.title

            # Step 2: Transcribe
            queue_item.status = "transcribing"
            segments = await self._transcribe(audio_path)
            full_transcript = self._segments_to_text(segments)

            # Step 3: Generate summary, key points, and soundbites
            queue_item.status = "summarizing"
            summarizer = self._get_summarizer()

            summary_result = await summarizer.summarize_podcast(
                transcript=full_transcript,
                title=metadata.title,
                show_name=metadata.show_name,
            )

            # Extract soundbites with timestamps
            soundbites = self._extract_soundbites(
                segments,
                summary_result.get("soundbites", []),
            )

            # Step 4: Generate embeddings and find connections
            embedder = self._get_embedder()
            embedding = embedder.embed(summary_result["summary"])

            # Find connections to existing content
            from src.storage.vectors import VectorStore
            vector_store = VectorStore(self.config.obsidian.vault_path / ".vectors.db")
            similar = vector_store.find_similar(embedding, top_k=5, exclude_id=item_id)

            connections = []
            for content, score in similar:
                if score > 0.5:  # Relevance threshold
                    connections.append(f"[[{content.vault_path}|{content.title}]] (similarity: {score:.2f})")

            # Step 5: Save to vault
            vault_path = self.vault.save_podcast(
                metadata=metadata,
                transcript=full_transcript,
                summary=summary_result["summary"],
                key_points=summary_result.get("key_points", []),
                soundbites=soundbites,
                connections=connections,
                tags=summary_result.get("tags", []),
            )

            # Store embedding for future connections
            vector_store.add(
                content_id=item_id,
                content_type="podcast",
                title=metadata.title,
                vault_path=vault_path,
                summary=summary_result["summary"],
                embedding=embedding,
            )

            # Add summary to scratchpad
            self.vault.save_content_to_scratchpad(
                content_type="podcast",
                title=metadata.title,
                summary=summary_result["summary"],
                vault_path=vault_path,
            )

            # Clean up audio file
            if audio_path.exists():
                audio_path.unlink()

            queue_item.status = "complete"

            duration = metadata.duration or 0
            return PodcastResult(
                title=metadata.title,
                show_name=metadata.show_name,
                duration=duration,
                duration_str=self._format_duration(duration),
                summary=summary_result["summary"],
                key_points=summary_result.get("key_points", []),
                soundbites=soundbites,
                vault_path=vault_path,
                transcript=full_transcript,
            )

        except Exception as e:
            queue_item.status = "error"
            queue_item.error = str(e)
            raise
        finally:
            # Remove from queue after a delay
            asyncio.create_task(self._remove_from_queue(item_id, delay=300))

    async def process_transcript_only(
        self, url: str, status_callback: Optional[callable] = None
    ) -> dict:
        """Process a podcast URL but only do transcription (no AI summary).

        Returns dict with transcript, metadata, and duration info.
        Used for interactive mode where user adds their own insights.

        Args:
            url: The podcast URL to process
            status_callback: Optional async callable(status_msg: str) for progress updates
        """
        item_id = hashlib.md5(url.encode()).hexdigest()[:8]
        queue_item = QueueItem(id=item_id, url=url, title="Processing...")
        self.queue.append(queue_item)

        async def report_status(msg: str):
            if status_callback:
                await status_callback(msg)

        try:
            # Step 1: Resolve URL and get audio
            queue_item.status = "downloading"
            await report_status("📥 **Step 1/3:** Downloading audio...")
            audio_path, metadata = await self._download_audio(url)
            queue_item.title = metadata.title

            duration = metadata.duration or 0
            duration_str = self._format_duration(duration)

            # Step 2: Transcribe
            queue_item.status = "transcribing"
            queue_item.duration_seconds = duration
            queue_item.started_at = datetime.now().timestamp()
            await report_status(
                f"🎙️ **Step 2/3:** Transcribing audio...\n"
                f"Duration: {duration_str}\n"
                f"_This may take a few minutes._"
            )
            segments = await self._transcribe(audio_path, status_callback=report_status)
            full_transcript = self._segments_to_text(segments)

            # Clean up audio file
            if audio_path.exists():
                audio_path.unlink()

            queue_item.status = "complete"
            await report_status("✅ **Step 3/3:** Transcription complete!")

            return {
                "transcript": full_transcript,
                "metadata": metadata,
                "duration": duration,
                "duration_str": duration_str,
                "segments": segments,
            }

        except Exception as e:
            queue_item.status = "error"
            queue_item.error = str(e)
            raise
        finally:
            # Unload Whisper model to free memory after transcription
            self.unload_whisper_model()
            # Remove from queue after delay (handle case where event loop might not be running)
            try:
                asyncio.create_task(self._remove_from_queue(item_id, delay=300))
            except RuntimeError:
                # No event loop running, just remove synchronously
                self.queue = [item for item in self.queue if item.id != item_id]

    async def _download_audio(self, url: str) -> tuple[Path, PodcastMetadata]:
        """Download audio from URL and extract metadata."""
        parsed = urlparse(url)

        # Check if it's an RSS feed
        if self._is_rss_url(url):
            return await self._download_from_rss(url)

        # Check if it's a Spotify link
        if "spotify.com" in parsed.netloc:
            return await self._download_from_spotify(url)

        # Check if it's Apple Podcasts
        if "podcasts.apple.com" in parsed.netloc:
            return await self._download_from_apple(url)

        # Try yt-dlp as fallback (works with many podcast hosts)
        return await self._download_with_ytdlp(url)

    def _is_rss_url(self, url: str) -> bool:
        """Check if URL appears to be an RSS feed."""
        rss_indicators = [".xml", "/feed", "/rss", "feed=", "format=rss"]
        return any(indicator in url.lower() for indicator in rss_indicators)

    async def _download_from_rss(self, feed_url: str, episode_index: int = 0, episode_title: str | None = None) -> tuple[Path, PodcastMetadata]:
        """Download from an RSS feed (most reliable method)."""
        logger.info(f"Parsing RSS feed: {feed_url}")

        # Fetch RSS content with httpx (handles SSL better than feedparser's urllib)
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(
                feed_url,
                follow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0 (compatible; KnowledgeBot/1.0)"},
            )
            response.raise_for_status()
            rss_content = response.text

        # Parse RSS feed from content
        feed = feedparser.parse(rss_content)

        if not feed.entries:
            raise ValueError("No episodes found in RSS feed")

        # If episode_title is provided, search for matching episode
        if episode_title:
            episode = self._find_episode_by_title(feed.entries, episode_title)
            if episode:
                logger.info(f"Matched episode by title: {episode.get('title', 'Unknown')}")
            else:
                logger.warning(f"Could not match episode '{episode_title}', using latest")
                episode = feed.entries[episode_index]
        else:
            episode = feed.entries[episode_index]

        # Extract metadata
        metadata = PodcastMetadata(
            title=episode.get("title", "Unknown Episode"),
            show_name=feed.feed.get("title"),
            date=self._parse_date(episode.get("published")),
            url=feed_url,
            description=episode.get("summary", ""),
        )

        # Find audio URL
        audio_url = None
        for link in episode.get("links", []):
            if link.get("type", "").startswith("audio/"):
                audio_url = link["href"]
                break

        # Try enclosures as fallback
        if not audio_url:
            for enclosure in episode.get("enclosures", []):
                if enclosure.get("type", "").startswith("audio/"):
                    audio_url = enclosure["url"]
                    break

        if not audio_url:
            raise ValueError("No audio URL found in RSS episode")

        # Extract duration from itunes:duration if available
        duration_str = episode.get("itunes_duration", "")
        if duration_str:
            metadata.duration = self._parse_duration(duration_str)

        # Download audio
        audio_path = await self._download_audio_file(audio_url, metadata.title)

        return audio_path, metadata

    def _find_episode_by_title(self, entries: list, target_title: str):
        """Find an RSS episode matching the target title."""
        target_lower = target_title.lower().strip()
        # Try exact match first
        for entry in entries:
            if entry.get("title", "").lower().strip() == target_lower:
                return entry
        # Try substring match (title might be truncated or have extra info)
        for entry in entries:
            entry_title = entry.get("title", "").lower().strip()
            if target_lower in entry_title or entry_title in target_lower:
                return entry
        # Try word-based matching (at least 60% of words match)
        target_words = set(target_lower.split())
        if len(target_words) >= 3:
            for entry in entries:
                entry_words = set(entry.get("title", "").lower().split())
                overlap = len(target_words & entry_words)
                if overlap >= len(target_words) * 0.6:
                    return entry
        return None

    async def _get_episode_title_from_spotify(self, episode_id: str) -> str | None:
        """Get the episode title from Spotify using oEmbed API."""
        try:
            episode_url = f"https://open.spotify.com/episode/{episode_id}"
            oembed_url = f"https://open.spotify.com/oembed?url={episode_url}"
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(oembed_url)
                if response.status_code == 200:
                    data = response.json()
                    title = data.get("title", "")
                    if title:
                        logger.info(f"Got episode title from Spotify: {title}")
                        return title
        except Exception as e:
            logger.debug(f"Failed to get episode title from Spotify: {e}")
        return None

    async def _download_from_spotify(self, url: str) -> tuple[Path, PodcastMetadata]:
        """Download from Spotify by finding the RSS feed or using yt-dlp."""
        logger.info(f"Processing Spotify URL: {url}")

        # Extract show ID and episode ID from Spotify URL
        # URLs look like: https://open.spotify.com/show/XXXXX or /episode/XXXXX
        parsed = urlparse(url)
        path_parts = parsed.path.strip("/").split("/")

        show_id = None
        episode_id = None

        for i, part in enumerate(path_parts):
            if part == "show" and i + 1 < len(path_parts):
                show_id = path_parts[i + 1].split("?")[0]
            elif part == "episode" and i + 1 < len(path_parts):
                episode_id = path_parts[i + 1].split("?")[0]

        # Get episode title for RSS matching if we have an episode ID
        episode_title = None
        if episode_id:
            episode_title = await self._get_episode_title_from_spotify(episode_id)
            logger.info(f"Target episode title from Spotify: {episode_title}")

        # Try to find RSS feed first
        if not show_id and episode_id:
            show_id = await self._get_show_id_from_episode(episode_id)

        rss_result = None
        rss_matched = False

        if show_id:
            rss_url = await self._find_rss_from_spotify(show_id)
            if rss_url:
                logger.info(f"Found RSS feed: {rss_url}")
                try:
                    rss_result = await self._download_from_rss(rss_url, episode_title=episode_title)
                    # Check if we got the right episode
                    if episode_title and rss_result[1].title:
                        rss_matched = self._titles_match(episode_title, rss_result[1].title)
                        if rss_matched:
                            logger.info(f"RSS episode matched: {rss_result[1].title}")
                            return rss_result
                        else:
                            logger.warning(f"RSS returned different episode: '{rss_result[1].title}' vs expected '{episode_title}'")
                    else:
                        # No episode title to match, assume RSS is correct
                        return rss_result
                except Exception as e:
                    logger.warning(f"RSS download failed: {e}")

        # If RSS didn't match the episode, try yt-dlp for exact episode
        if episode_id and not rss_matched:
            logger.info("Trying yt-dlp for exact episode match")
            try:
                result = await self._download_with_ytdlp(url)
                logger.info("Successfully downloaded via yt-dlp")
                return result
            except Exception as e:
                logger.warning(f"yt-dlp failed: {e}")
                # If we have an RSS result (even if wrong episode), use it as last resort
                if rss_result:
                    logger.warning("Using RSS result (may be wrong episode) as yt-dlp also failed")
                    return rss_result
                # Both failed
                if "DRM" in str(e):
                    raise ValueError(
                        "This podcast is DRM-protected on Spotify and the RSS feed "
                        "doesn't have this specific episode. Try one of these:\n"
                        "1. Find the podcast's RSS feed URL on their website\n"
                        "2. Search '[podcast name] RSS feed' online\n"
                        "3. Try a different episode from this show\n\n"
                        "Then use: /podcast <rss-feed-url>"
                    )
                raise

        # No episode ID (show URL) - just get latest from RSS or try yt-dlp
        if rss_result:
            return rss_result

        try:
            return await self._download_with_ytdlp(url)
        except Exception as e:
            if "DRM" in str(e):
                raise ValueError(
                    "This podcast is DRM-protected on Spotify. "
                    "Try finding the podcast's RSS feed URL."
                )
            raise

    def _titles_match(self, title1: str, title2: str) -> bool:
        """Check if two episode titles are similar enough to be the same episode."""
        t1 = title1.lower().strip()
        t2 = title2.lower().strip()
        # Exact match
        if t1 == t2:
            return True
        # One contains the other
        if t1 in t2 or t2 in t1:
            return True
        # Word overlap (at least 50% of words match)
        words1 = set(t1.split())
        words2 = set(t2.split())
        if words1 and words2:
            overlap = len(words1 & words2)
            min_words = min(len(words1), len(words2))
            if overlap >= min_words * 0.5:
                return True
        return False

    async def _get_show_id_from_episode(self, episode_id: str) -> str | None:
        """Get the show ID from a Spotify episode by fetching the episode page."""
        try:
            episode_url = f"https://open.spotify.com/episode/{episode_id}"
            async with httpx.AsyncClient(timeout=15.0) as client:
                # Fetch the episode page - Spotify embeds show info in the HTML
                response = await client.get(
                    episode_url,
                    follow_redirects=True,
                    headers={
                        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
                    },
                )

                if response.status_code != 200:
                    logger.debug(f"Failed to fetch episode page: {response.status_code}")
                    return None

                html = response.text

                # Look for show link in the HTML
                # Pattern: /show/[show_id] appears in the page
                show_pattern = r'/show/([a-zA-Z0-9]{22})'
                match = re.search(show_pattern, html)

                if match:
                    show_id = match.group(1)
                    logger.info(f"Extracted show ID from episode page: {show_id}")
                    return show_id

                # Try oEmbed API as fallback
                oembed_url = f"https://open.spotify.com/oembed?url={episode_url}"
                oembed_response = await client.get(oembed_url)
                if oembed_response.status_code == 200:
                    data = oembed_response.json()
                    # The HTML in oEmbed might contain show link
                    oembed_html = data.get("html", "")
                    match = re.search(show_pattern, oembed_html)
                    if match:
                        return match.group(1)

        except Exception as e:
            logger.debug(f"Failed to get show ID from episode: {e}")

        return None

    async def _get_podcast_name_from_spotify(self, show_id: str) -> str | None:
        """Get the podcast name from Spotify show page."""
        try:
            show_url = f"https://open.spotify.com/show/{show_id}"
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.get(
                    show_url,
                    follow_redirects=True,
                    headers={
                        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
                    },
                )
                if response.status_code == 200:
                    # Look for the title in meta tags or og:title
                    title_match = re.search(r'<title>([^<]+)</title>', response.text)
                    if title_match:
                        title = title_match.group(1)
                        # Clean up " | Podcast on Spotify" suffix
                        title = re.sub(r'\s*[|\-]\s*Podcast.*$', '', title, flags=re.IGNORECASE)
                        title = re.sub(r'\s*[|\-]\s*Spotify.*$', '', title, flags=re.IGNORECASE)
                        return title.strip()
        except Exception as e:
            logger.debug(f"Failed to get podcast name: {e}")
        return None

    async def _find_real_rss_from_itunes(self, podcast_name: str) -> str | None:
        """Search iTunes for the podcast and get the real RSS feed URL.

        Uses exact match first, then falls back to substring match.
        This handles generic podcast names like "Empire" by preferring exact matches.
        """
        try:
            search_url = "https://itunes.apple.com/search"
            params = {
                "term": podcast_name,
                "entity": "podcast",
                "limit": 10,
            }
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.get(search_url, params=params)
                if response.status_code == 200:
                    data = response.json()
                    results = data.get("results", [])
                    podcast_lower = podcast_name.lower().strip()

                    # Priority 1: Exact name match (handles generic names like "Empire")
                    for result in results:
                        name = result.get("collectionName", "").lower().strip()
                        feed_url = result.get("feedUrl")
                        if feed_url and name == podcast_lower:
                            logger.info(f"Found exact RSS match: {feed_url}")
                            return feed_url

                    # Priority 2: Substring match (podcast name appears in result)
                    for result in results:
                        name = result.get("collectionName", "").lower()
                        feed_url = result.get("feedUrl")
                        if feed_url and podcast_lower in name:
                            logger.info(f"Found RSS via substring: {feed_url}")
                            return feed_url

                    # Priority 3: First result with a feed (best guess)
                    for result in results:
                        feed_url = result.get("feedUrl")
                        if feed_url:
                            logger.info(f"Found RSS feed via iTunes (best guess): {feed_url}")
                            return feed_url

        except Exception as e:
            logger.debug(f"iTunes lookup failed: {e}")
        return None

    async def _find_rss_from_spotify(self, show_id: str) -> str | None:
        """Try to find RSS feed for a Spotify show using various methods."""
        # Method 1: Get podcast name and search iTunes for the real RSS feed
        # This is preferred because it gets the actual audio URLs
        podcast_name = await self._get_podcast_name_from_spotify(show_id)
        if podcast_name:
            logger.info(f"Found podcast name: {podcast_name}")
            real_rss = await self._find_real_rss_from_itunes(podcast_name)
            if real_rss:
                # Verify this RSS has audio enclosures
                if await self._rss_has_audio(real_rss):
                    return real_rss

        # Method 2: Try spotifeed as fallback (but note: it often lacks audio URLs)
        spotifeed_url = f"https://spotifeed.timdorr.com/{show_id}"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.head(spotifeed_url, follow_redirects=True)
                if response.status_code == 200:
                    # Check if spotifeed actually has audio
                    if await self._rss_has_audio(spotifeed_url):
                        return spotifeed_url
                    else:
                        logger.debug("Spotifeed has no audio URLs, skipping")
        except Exception as e:
            logger.debug(f"Spotifeed lookup failed: {e}")

        return None

    async def _rss_has_audio(self, feed_url: str) -> bool:
        """Check if an RSS feed has audio enclosures."""
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.get(
                    feed_url,
                    follow_redirects=True,
                    headers={"User-Agent": "Mozilla/5.0 (compatible; KnowledgeBot/1.0)"},
                )
                if response.status_code == 200:
                    # Quick check for enclosure tags with audio
                    content = response.text
                    return 'type="audio' in content or "type='audio" in content
        except Exception as e:
            logger.debug(f"Failed to check RSS for audio: {e}")
        return False

    async def _download_from_apple(self, url: str) -> tuple[Path, PodcastMetadata]:
        """Download from Apple Podcasts."""
        logger.info(f"Processing Apple Podcasts URL: {url}")

        # yt-dlp supports Apple Podcasts
        return await self._download_with_ytdlp(url)

    async def _download_with_ytdlp(self, url: str) -> tuple[Path, PodcastMetadata]:
        """Download using yt-dlp (supports many sources including video podcasts)."""
        import yt_dlp

        temp_dir = Path(tempfile.mkdtemp())
        output_template = str(temp_dir / "%(title)s.%(ext)s")

        # Format selection prioritizes audio-only, falls back to extracting from video
        # For video podcasts (Spotify, YouTube), this extracts just the audio track
        ydl_opts = {
            "format": "bestaudio[ext=m4a]/bestaudio[ext=mp3]/bestaudio/best[acodec!=none]",
            "outtmpl": output_template,
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }
            ],
            "quiet": True,
            "no_warnings": True,
            # Avoid downloading video when audio is available
            "extract_audio": True,
            # For Spotify: ensure we get the right episode by using the URL directly
            "noplaylist": True,
            # Better error messages
            "ignoreerrors": False,
        }

        loop = asyncio.get_event_loop()

        def download():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                return info

        info = await loop.run_in_executor(None, download)

        # Find the downloaded file
        audio_files = list(temp_dir.glob("*.mp3")) + list(temp_dir.glob("*.m4a")) + list(temp_dir.glob("*.opus")) + list(temp_dir.glob("*.webm"))
        if not audio_files:
            raise ValueError("Failed to download audio - no audio file found after extraction")

        audio_path = audio_files[0]

        # For video podcasts, the title might include extra info - clean it up
        title = info.get("title", "Unknown")
        # Remove common video podcast suffixes
        title = re.sub(r'\s*[\|\-]\s*(Video|Full Episode|Official).*$', '', title, flags=re.IGNORECASE)

        metadata = PodcastMetadata(
            title=title,
            show_name=info.get("series") or info.get("uploader") or info.get("channel"),
            date=info.get("upload_date"),
            duration=info.get("duration"),
            url=url,
            description=info.get("description", ""),
        )

        return audio_path, metadata

    async def _download_audio_file(self, url: str, title: str) -> Path:
        """Download an audio file from a direct URL."""
        temp_dir = Path(tempfile.mkdtemp())
        safe_title = re.sub(r'[<>:"/\\|?*]', "", title)[:50]
        audio_path = temp_dir / f"{safe_title}.mp3"

        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        }
        timeout = httpx.Timeout(10.0, read=300.0)  # 5 min read timeout for large files
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, headers=headers) as client:
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                with open(audio_path, "wb") as f:
                    async for chunk in response.aiter_bytes(chunk_size=65536):
                        f.write(chunk)

        logger.info(f"Downloaded audio: {audio_path.stat().st_size / (1024*1024):.1f}MB")
        return audio_path

    async def _transcribe(self, audio_path: Path, status_callback=None) -> list[TranscriptSegment]:
        """Transcribe audio file using Whisper (local or cloud)."""
        logger.info(f"Transcribing: {audio_path}")

        # Check if we should use cloud transcription
        if self.config.whisper.mode == "cloud":
            return await self._transcribe_cloud(audio_path, status_callback=status_callback)
        else:
            return await self._transcribe_local(audio_path)

    async def _compress_audio_for_cloud(self, audio_path: Path) -> Path:
        """Compress audio to under 25MB for OpenAI API limit."""
        import subprocess

        file_size_mb = audio_path.stat().st_size / (1024 * 1024)
        logger.info(f"Original audio size: {file_size_mb:.1f}MB")

        # OpenAI limit is 25MB, target 18MB to be safe
        if file_size_mb <= 18:
            return audio_path

        # Compress using ffmpeg - mono, 16kHz (Whisper's native rate), low bitrate
        compressed_path = audio_path.with_suffix('.compressed.mp3')

        # Start with 24k bitrate, go lower if file is very large
        if file_size_mb > 100:
            target_bitrate = "16k"  # Very long podcasts need more compression
        elif file_size_mb > 50:
            target_bitrate = "20k"
        else:
            target_bitrate = "24k"

        cmd = [
            "ffmpeg", "-y", "-i", str(audio_path),
            "-ac", "1",           # Mono
            "-ar", "16000",       # 16kHz (Whisper's native sample rate)
            "-b:a", target_bitrate,
            "-map", "0:a",        # Audio only
            str(compressed_path)
        ]

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            lambda: subprocess.run(cmd, capture_output=True, check=True)
        )

        compressed_size_mb = compressed_path.stat().st_size / (1024 * 1024)
        logger.info(f"Compressed audio size: {compressed_size_mb:.1f}MB (bitrate: {target_bitrate})")

        # If still too large, try again with even lower bitrate
        if compressed_size_mb > 24:
            logger.warning(f"Still too large ({compressed_size_mb:.1f}MB), recompressing with 12k bitrate")
            recompressed_path = audio_path.with_suffix('.recompressed.mp3')
            cmd[10] = "12k"  # Lower bitrate
            cmd[-1] = str(recompressed_path)
            await loop.run_in_executor(
                None,
                lambda: subprocess.run(cmd, capture_output=True, check=True)
            )
            if recompressed_path.exists():
                compressed_path.unlink()
                compressed_path = recompressed_path
                compressed_size_mb = compressed_path.stat().st_size / (1024 * 1024)
                logger.info(f"Recompressed audio size: {compressed_size_mb:.1f}MB")

        # Clean up original if compression worked
        if compressed_path.exists() and compressed_size_mb < file_size_mb:
            audio_path.unlink()
            return compressed_path

        return audio_path

    def _get_openai_fallback_key(self) -> str:
        """Get a real OpenAI key for fallback (not a Groq key).

        Checks OPENAI_WHISPER_KEY env var first, then config.whisper.openai_api_key.
        """
        import os
        # Dedicated fallback env var (avoids Railway variable editing issues)
        key = os.environ.get("OPENAI_WHISPER_KEY", "").strip()
        if key and not key.startswith("gsk_"):
            return key
        # Config-loaded key
        key = self.config.whisper.openai_api_key
        if key and not key.startswith("gsk_"):
            return key
        return ""

    def _has_openai_fallback(self) -> bool:
        """Check if a real OpenAI key is available for fallback."""
        return bool(self._get_openai_fallback_key())

    async def _transcribe_cloud(self, audio_path: Path, status_callback=None) -> list[TranscriptSegment]:
        """Transcribe using Groq or OpenAI Whisper API (fast, cloud-based).

        If Groq fails with 429 (rate limit) or 413 (file too large), automatically
        falls back to OpenAI if a real OpenAI key is configured.
        """
        import openai

        use_groq = bool(self.config.whisper.groq_api_key)

        # Compress audio for cloud APIs (25MB limit)
        audio_path = await self._compress_audio_for_cloud(audio_path)

        if use_groq:
            try:
                return await self._call_whisper_api(
                    audio_path,
                    api_key=self.config.whisper.groq_api_key,
                    base_url="https://api.groq.com/openai/v1",
                    model="whisper-large-v3-turbo",
                    provider="Groq",
                )
            except (openai.RateLimitError, openai.APIStatusError) as e:
                status_code = getattr(e, 'status_code', None)
                if status_code == 429 and self._has_openai_fallback():
                    logger.warning(f"Groq rate limited, falling back to OpenAI: {e}")
                    if status_callback:
                        await status_callback("⚡ Groq rate limit hit — switching to OpenAI for this transcription...")
                elif status_code == 413 and self._has_openai_fallback():
                    logger.warning(f"Groq file too large, falling back to OpenAI: {e}")
                    if status_callback:
                        await status_callback("⚡ Audio too large for Groq — switching to OpenAI for this transcription...")
                else:
                    raise

            # Fallback to OpenAI
            fallback_key = self._get_openai_fallback_key()
            logger.info("Falling back to OpenAI Whisper API")
            return await self._call_whisper_api(
                audio_path,
                api_key=fallback_key,
                base_url=None,
                model="whisper-1",
                provider="OpenAI (fallback)",
            )
        else:
            # No Groq key — use OpenAI directly (check OPENAI_WHISPER_KEY too)
            openai_key = self._get_openai_fallback_key() or self.config.whisper.openai_api_key
            return await self._call_whisper_api(
                audio_path,
                api_key=openai_key,
                base_url=None,
                model="whisper-1",
                provider="OpenAI",
            )

    async def _call_whisper_api(
        self,
        audio_path: Path,
        api_key: str,
        base_url: str | None,
        model: str,
        provider: str,
    ) -> list[TranscriptSegment]:
        """Call a Whisper-compatible API and return transcript segments."""
        import openai

        logger.info(f"Using {provider} Whisper API for transcription")

        client_kwargs = {"api_key": api_key}
        if base_url:
            client_kwargs["base_url"] = base_url
        client = openai.OpenAI(**client_kwargs)

        loop = asyncio.get_event_loop()

        def run_cloud_transcription():
            with open(audio_path, "rb") as audio_file:
                response = client.audio.transcriptions.create(
                    model=model,
                    file=audio_file,
                    response_format="verbose_json",
                    timestamp_granularities=["segment"],
                )
            return response

        response = await loop.run_in_executor(None, run_cloud_transcription)

        # Convert response to TranscriptSegment format
        segments = []
        if hasattr(response, 'segments') and response.segments:
            for seg in response.segments:
                text = seg.get('text', '').strip() if isinstance(seg, dict) else getattr(seg, 'text', '').strip()
                start = seg.get('start', 0) if isinstance(seg, dict) else getattr(seg, 'start', 0)
                end = seg.get('end', 0) if isinstance(seg, dict) else getattr(seg, 'end', 0)
                if text:
                    segments.append(TranscriptSegment(text=text, start=start, end=end))
        else:
            # Fallback: create single segment from full text
            text = response.text if hasattr(response, 'text') else str(response)
            segments.append(TranscriptSegment(text=text.strip(), start=0, end=0))

        logger.info(f"{provider} transcribed {len(segments)} segments")
        return segments

    async def _transcribe_local(self, audio_path: Path) -> list[TranscriptSegment]:
        """Transcribe using local faster-whisper (slower, no API cost)."""
        model = self._get_whisper_model()

        loop = asyncio.get_event_loop()

        def run_transcription():
            segments, info = model.transcribe(
                str(audio_path),
                beam_size=5,
                word_timestamps=True,
            )
            return list(segments), info

        segments_raw, info = await loop.run_in_executor(None, run_transcription)

        segments = [
            TranscriptSegment(
                text=seg.text.strip(),
                start=seg.start,
                end=seg.end,
            )
            for seg in segments_raw
            if seg.text.strip()
        ]

        logger.info(f"Local transcribed {len(segments)} segments")
        return segments

    def _segments_to_text(self, segments: list[TranscriptSegment]) -> str:
        """Convert segments to plain text transcript."""
        return " ".join(seg.text for seg in segments)

    def _extract_soundbites(
        self,
        segments: list[TranscriptSegment],
        soundbite_texts: list[dict],
    ) -> list[Soundbite]:
        """Match soundbite texts to their timestamps in the transcript."""
        soundbites = []

        for sb_data in soundbite_texts:
            text = sb_data.get("text", "")
            speaker = sb_data.get("speaker")
            context = sb_data.get("context")

            # Find the timestamp by matching text to segments
            timestamp = self._find_timestamp_for_text(segments, text)

            soundbites.append(
                Soundbite(
                    text=text,
                    timestamp=timestamp,
                    speaker=speaker,
                    context=context,
                )
            )

        return soundbites

    def _find_timestamp_for_text(
        self,
        segments: list[TranscriptSegment],
        text: str,
    ) -> Optional[str]:
        """Find the timestamp where a piece of text appears."""
        # Normalize the search text
        search_text = text.lower().strip()
        words = search_text.split()[:5]  # Use first 5 words for matching

        for seg in segments:
            seg_text = seg.text.lower()
            # Check if the segment contains the start of the quote
            if all(word in seg_text for word in words):
                return seg.timestamp

        return None

    def _parse_date(self, date_str: Optional[str]) -> Optional[str]:
        """Parse various date formats to YYYY-MM-DD."""
        if not date_str:
            return None

        from email.utils import parsedate_to_datetime

        try:
            dt = parsedate_to_datetime(date_str)
            return dt.strftime("%Y-%m-%d")
        except Exception:
            pass

        # Try common formats
        for fmt in ["%Y-%m-%d", "%Y/%m/%d", "%d %b %Y", "%B %d, %Y"]:
            try:
                dt = datetime.strptime(date_str, fmt)
                return dt.strftime("%Y-%m-%d")
            except ValueError:
                continue

        return None

    def _parse_duration(self, duration_str: str) -> int:
        """Parse duration string to seconds."""
        parts = duration_str.split(":")
        try:
            if len(parts) == 3:  # HH:MM:SS
                return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
            elif len(parts) == 2:  # MM:SS
                return int(parts[0]) * 60 + int(parts[1])
            else:
                return int(duration_str)
        except ValueError:
            return 0

    def _format_duration(self, seconds: int) -> str:
        """Format seconds as human-readable duration."""
        hours, remainder = divmod(seconds, 3600)
        minutes, secs = divmod(remainder, 60)

        if hours:
            return f"{hours}h {minutes}m"
        return f"{minutes}m"

    def get_queue_status(self) -> list[dict]:
        """Get the current processing queue status."""
        return [
            {
                "id": item.id,
                "title": item.title,
                "status": item.status,
                "progress": item.progress,
                "error": item.error,
                "started_at": item.started_at,
                "duration_seconds": item.duration_seconds,
            }
            for item in self.queue
        ]

    async def _remove_from_queue(self, item_id: str, delay: float = 0) -> None:
        """Remove an item from the queue after a delay."""
        if delay:
            await asyncio.sleep(delay)
        self.queue = [item for item in self.queue if item.id != item_id]
