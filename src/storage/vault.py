"""Obsidian vault writer for storing content as markdown files."""

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional


@dataclass
class Soundbite:
    """A notable quote or soundbite from content."""

    text: str
    timestamp: Optional[str] = None  # For podcasts: "12:34"
    speaker: Optional[str] = None
    context: Optional[str] = None


@dataclass
class PodcastMetadata:
    """Metadata for a podcast episode."""

    title: str
    show_name: Optional[str] = None
    date: Optional[str] = None
    duration: Optional[int] = None  # seconds
    url: Optional[str] = None
    description: Optional[str] = None


@dataclass
class ArticleMetadata:
    """Metadata for an article."""

    title: str
    author: Optional[str] = None
    date: Optional[str] = None
    url: Optional[str] = None
    site_name: Optional[str] = None


@dataclass
class ThreadMetadata:
    """Metadata for an X/Twitter thread."""

    author: str
    date: Optional[str] = None
    url: Optional[str] = None
    tweet_count: int = 0


class VaultWriter:
    """Handles writing content to an Obsidian vault."""

    def __init__(self, vault_path: Path | str):
        self.vault_path = Path(vault_path)
        self._ensure_structure()

    def _ensure_structure(self) -> None:
        """Ensure the vault directory structure exists."""
        directories = [
            "daily",
            "content/podcasts",
            "content/articles",
            "content/threads",
            "notes",
            "insights",
            "templates",
        ]
        for dir_name in directories:
            (self.vault_path / dir_name).mkdir(parents=True, exist_ok=True)

    def _sanitize_filename(self, name: str) -> str:
        """Convert a title to a safe filename."""
        # Remove or replace problematic characters
        name = re.sub(r'[<>:"/\\|?*]', "", name)
        name = re.sub(r"\s+", "-", name)
        name = name.lower()[:50]  # Limit length
        return name

    def _format_timestamp(self, seconds: int) -> str:
        """Format seconds as HH:MM:SS or MM:SS."""
        hours, remainder = divmod(seconds, 3600)
        minutes, secs = divmod(remainder, 60)
        if hours:
            return f"{hours:02d}:{minutes:02d}:{secs:02d}"
        return f"{minutes:02d}:{secs:02d}"

    def _format_wikilinks(self, tags: list[str]) -> str:
        """Format a list of tags as wikilinks."""
        return " ".join(f"[[{tag}]]" for tag in tags)

    def save_podcast(
        self,
        metadata: PodcastMetadata,
        transcript: str,
        summary: str,
        key_points: list[str],
        soundbites: list[Soundbite],
        connections: list[str] | None = None,
        tags: list[str] | None = None,
    ) -> str:
        """Save a podcast episode to the vault.

        Returns the relative path to the saved file.
        """
        date_str = metadata.date or datetime.now().strftime("%Y-%m-%d")
        filename = f"{self._sanitize_filename(metadata.title)}-{date_str}.md"
        filepath = self.vault_path / "content" / "podcasts" / filename

        # Build frontmatter
        frontmatter = [
            "---",
            f'title: "{metadata.title}"',
            f"type: podcast",
            f"date: {date_str}",
        ]
        if metadata.show_name:
            frontmatter.append(f'show: "{metadata.show_name}"')
        if metadata.duration:
            frontmatter.append(f"duration: {self._format_timestamp(metadata.duration)}")
        if metadata.url:
            frontmatter.append(f"url: {metadata.url}")
        if tags:
            frontmatter.append(f"tags: [{', '.join(tags)}]")
        frontmatter.append("---\n")

        # Build content
        content_parts = ["\n".join(frontmatter)]

        # Summary section
        content_parts.append(f"# {metadata.title}\n")
        if metadata.show_name:
            content_parts.append(f"**Show:** {metadata.show_name}\n")
        if metadata.duration:
            content_parts.append(f"**Duration:** {self._format_timestamp(metadata.duration)}\n")
        content_parts.append(f"\n## Summary\n\n{summary}\n")

        # Key points
        if key_points:
            content_parts.append("\n## Key Points\n")
            for point in key_points:
                content_parts.append(f"- {point}")
            content_parts.append("")

        # Soundbites
        if soundbites:
            content_parts.append("\n## Notable Quotes & Soundbites\n")
            for sb in soundbites:
                quote_block = f"> {sb.text}"
                if sb.speaker:
                    quote_block += f"\n> — **{sb.speaker}**"
                if sb.timestamp:
                    quote_block += f" ({sb.timestamp})"
                if sb.context:
                    quote_block += f"\n\n_{sb.context}_"
                content_parts.append(quote_block + "\n")

        # Connections
        if connections:
            content_parts.append("\n## Connections\n")
            for conn in connections:
                content_parts.append(f"- {conn}")
            content_parts.append("")

        # Full transcript (collapsible)
        content_parts.append("\n## Full Transcript\n")
        content_parts.append("<details>")
        content_parts.append("<summary>Click to expand transcript</summary>\n")
        content_parts.append(transcript)
        content_parts.append("\n</details>")

        # Write file
        filepath.write_text("\n".join(content_parts))

        return str(filepath.relative_to(self.vault_path))

    def save_podcast_email(
        self,
        metadata: PodcastMetadata,
        email_content: str,
        transcript: str,
    ) -> str:
        """Save a podcast with email-style summary to the vault.

        Args:
            metadata: PodcastMetadata object
            email_content: The email-style summary content
            transcript: Full transcript for reference

        Returns the relative path to the saved file.
        """
        date_str = metadata.date or datetime.now().strftime("%Y-%m-%d")
        filename = f"{self._sanitize_filename(metadata.title)}-{date_str}.md"
        filepath = self.vault_path / "content" / "podcasts" / filename

        # Build frontmatter
        frontmatter = [
            "---",
            f'title: "{metadata.title}"',
            f"type: podcast",
            f"date: {date_str}",
        ]
        if metadata.show_name:
            frontmatter.append(f'show: "{metadata.show_name}"')
        if metadata.duration:
            frontmatter.append(f"duration: {self._format_timestamp(metadata.duration)}")
        if metadata.url:
            frontmatter.append(f"url: {metadata.url}")
        frontmatter.append("---\n")

        # Build content
        content_parts = ["\n".join(frontmatter)]

        # Header
        content_parts.append(f"# {metadata.title}\n")
        if metadata.show_name:
            content_parts.append(f"**Show:** {metadata.show_name}")
        if metadata.duration:
            content_parts.append(f"**Duration:** {self._format_timestamp(metadata.duration)}")
        if metadata.url:
            content_parts.append(f"**Link:** [{metadata.url}]({metadata.url})")
        content_parts.append("")

        # Email content (the main summary)
        content_parts.append("\n## Summary\n")
        content_parts.append(email_content)
        content_parts.append("")

        # Full transcript (collapsible)
        content_parts.append("\n## Full Transcript\n")
        content_parts.append("<details>")
        content_parts.append("<summary>Click to expand transcript</summary>\n")
        content_parts.append(transcript)
        content_parts.append("\n</details>")

        # Write file
        filepath.write_text("\n".join(content_parts))

        return str(filepath.relative_to(self.vault_path))

    def save_article(
        self,
        metadata: ArticleMetadata,
        content: str,
        summary: str,
        key_points: list[str],
        connections: list[str] | None = None,
        tags: list[str] | None = None,
    ) -> str:
        """Save an article to the vault.

        Returns the relative path to the saved file.
        """
        date_str = metadata.date or datetime.now().strftime("%Y-%m-%d")
        filename = f"{self._sanitize_filename(metadata.title)}-{date_str}.md"
        filepath = self.vault_path / "content" / "articles" / filename

        # Build frontmatter
        frontmatter = [
            "---",
            f'title: "{metadata.title}"',
            f"type: article",
            f"date: {date_str}",
        ]
        if metadata.author:
            frontmatter.append(f'author: "{metadata.author}"')
        if metadata.site_name:
            frontmatter.append(f'source: "{metadata.site_name}"')
        if metadata.url:
            frontmatter.append(f"url: {metadata.url}")
        if tags:
            frontmatter.append(f"tags: [{', '.join(tags)}]")
        frontmatter.append("---\n")

        # Build content
        content_parts = ["\n".join(frontmatter)]

        content_parts.append(f"# {metadata.title}\n")
        if metadata.author:
            content_parts.append(f"**Author:** {metadata.author}")
        if metadata.site_name:
            content_parts.append(f"**Source:** {metadata.site_name}")
        if metadata.url:
            content_parts.append(f"**Link:** [{metadata.url}]({metadata.url})")
        content_parts.append("")

        content_parts.append(f"\n## Summary\n\n{summary}\n")

        if key_points:
            content_parts.append("\n## Key Points\n")
            for point in key_points:
                content_parts.append(f"- {point}")
            content_parts.append("")

        if connections:
            content_parts.append("\n## Connections\n")
            for conn in connections:
                content_parts.append(f"- {conn}")
            content_parts.append("")

        content_parts.append("\n## Full Content\n")
        content_parts.append(content)

        filepath.write_text("\n".join(content_parts))
        return str(filepath.relative_to(self.vault_path))

    def save_thread(
        self,
        metadata: ThreadMetadata,
        tweets: list[str],
        summary: str,
        connections: list[str] | None = None,
        tags: list[str] | None = None,
    ) -> str:
        """Save an X/Twitter thread to the vault.

        Returns the relative path to the saved file.
        """
        date_str = metadata.date or datetime.now().strftime("%Y-%m-%d")
        filename = f"thread-{metadata.author}-{date_str}.md"
        filepath = self.vault_path / "content" / "threads" / filename

        # Build frontmatter
        frontmatter = [
            "---",
            f'author: "@{metadata.author}"',
            f"type: thread",
            f"date: {date_str}",
            f"tweet_count: {metadata.tweet_count}",
        ]
        if metadata.url:
            frontmatter.append(f"url: {metadata.url}")
        if tags:
            frontmatter.append(f"tags: [{', '.join(tags)}]")
        frontmatter.append("---\n")

        # Build content
        content_parts = ["\n".join(frontmatter)]

        content_parts.append(f"# Thread by @{metadata.author}\n")
        content_parts.append(f"**Tweets:** {metadata.tweet_count}")
        if metadata.url:
            content_parts.append(f"**Link:** [{metadata.url}]({metadata.url})")
        content_parts.append("")

        content_parts.append(f"\n## Summary\n\n{summary}\n")

        if connections:
            content_parts.append("\n## Connections\n")
            for conn in connections:
                content_parts.append(f"- {conn}")
            content_parts.append("")

        content_parts.append("\n## Thread\n")
        for i, tweet in enumerate(tweets, 1):
            content_parts.append(f"**{i}.** {tweet}\n")

        filepath.write_text("\n".join(content_parts))
        return str(filepath.relative_to(self.vault_path))

    def _get_scratchpad_path(self) -> Path:
        """Get the path to the scratchpad file."""
        return self.vault_path / "scratchpad.md"

    def _ensure_scratchpad_exists(self) -> None:
        """Ensure the scratchpad file exists with proper header."""
        filepath = self._get_scratchpad_path()
        if not filepath.exists():
            header = """---
type: scratchpad
---

# Scratchpad

A running collection of notes, insights, and learnings.

---

"""
            filepath.write_text(header)

    def save_note(self, text: str, source: str | None = None) -> str:
        """Append a quick note to the scratchpad.

        Returns the relative path to the scratchpad file.
        """
        self._ensure_scratchpad_exists()
        filepath = self._get_scratchpad_path()

        timestamp = datetime.now()
        time_str = timestamp.strftime("%Y-%m-%d %H:%M")

        # Build the note entry
        entry_parts = [f"\n## 📝 Note - {time_str}\n"]
        if source:
            entry_parts.append(f"*Source: {source}*\n")
        entry_parts.append(f"\n{text}\n")
        entry_parts.append("\n---\n")

        # Append to scratchpad
        with open(filepath, "a") as f:
            f.write("".join(entry_parts))

        return "scratchpad.md"

    def save_insight(self, text: str, related_content: list[str] | None = None) -> str:
        """Append a key insight to the scratchpad.

        Returns the relative path to the scratchpad file.
        """
        self._ensure_scratchpad_exists()
        filepath = self._get_scratchpad_path()

        timestamp = datetime.now()
        time_str = timestamp.strftime("%Y-%m-%d %H:%M")

        # Build the insight entry
        entry_parts = [f"\n## 💡 Insight - {time_str}\n"]
        entry_parts.append(f"\n{text}\n")

        if related_content:
            entry_parts.append("\n**Related:**\n")
            for item in related_content:
                entry_parts.append(f"- [[{item}]]\n")

        entry_parts.append("\n---\n")

        # Append to scratchpad
        with open(filepath, "a") as f:
            f.write("".join(entry_parts))

        return "scratchpad.md"

    def save_content_to_scratchpad(
        self,
        content_type: str,
        title: str,
        summary: str,
        vault_path: str,
    ) -> None:
        """Add a content summary to the scratchpad.

        Called after processing podcasts, articles, or threads.
        """
        self._ensure_scratchpad_exists()
        filepath = self._get_scratchpad_path()

        timestamp = datetime.now()
        time_str = timestamp.strftime("%Y-%m-%d %H:%M")

        emoji = {"podcast": "🎙️", "article": "📰", "thread": "🧵"}.get(content_type, "📄")

        entry_parts = [f"\n## {emoji} {content_type.title()}: {title} - {time_str}\n"]
        entry_parts.append(f"\n*Full content: [[{vault_path}]]*\n")
        entry_parts.append(f"\n**Summary:** {summary}\n")
        entry_parts.append("\n---\n")

        with open(filepath, "a") as f:
            f.write("".join(entry_parts))

    def save_daily_digest(
        self,
        date: datetime,
        summary: str,
        content_processed: list[dict],
        themes: list[str],
        connections: list[str],
    ) -> str:
        """Save a daily digest to the vault.

        Returns the relative path to the saved file.
        """
        date_str = date.strftime("%Y-%m-%d")
        filename = f"{date_str}.md"
        filepath = self.vault_path / "daily" / filename

        frontmatter = [
            "---",
            f"type: daily",
            f"date: {date_str}",
            "---\n",
        ]

        content_parts = ["\n".join(frontmatter)]
        content_parts.append(f"# Daily Digest - {date_str}\n")

        content_parts.append("## Summary\n")
        content_parts.append(summary)
        content_parts.append("")

        if content_processed:
            content_parts.append("\n## Content Processed\n")
            for item in content_processed:
                item_type = item.get("type", "content")
                title = item.get("title", "Untitled")
                path = item.get("path", "")
                content_parts.append(f"- **{item_type}**: [[{path}|{title}]]")
            content_parts.append("")

        if themes:
            content_parts.append("\n## Key Themes\n")
            for theme in themes:
                content_parts.append(f"- {theme}")
            content_parts.append("")

        if connections:
            content_parts.append("\n## Connections & Insights\n")
            for conn in connections:
                content_parts.append(f"- {conn}")

        filepath.write_text("\n".join(content_parts))
        return str(filepath.relative_to(self.vault_path))

    def get_recent_content(self, days: int = 7, content_type: str | None = None) -> list[Path]:
        """Get recently saved content files."""
        from datetime import timedelta

        cutoff = datetime.now() - timedelta(days=days)
        results = []

        search_dirs = ["content/podcasts", "content/articles", "content/threads"]
        if content_type:
            search_dirs = [f"content/{content_type}s"]

        for dir_name in search_dirs:
            dir_path = self.vault_path / dir_name
            if dir_path.exists():
                for file in dir_path.glob("*.md"):
                    if file.stat().st_mtime > cutoff.timestamp():
                        results.append(file)

        return sorted(results, key=lambda p: p.stat().st_mtime, reverse=True)

    def list_podcast_summaries(self, limit: int = 10) -> list[dict]:
        """Get a list of recent podcast summaries.

        Returns list of dicts with: title, show, date, filepath
        """
        podcast_dir = self.vault_path / "content" / "podcasts"
        if not podcast_dir.exists():
            return []

        results = []
        for file in podcast_dir.glob("*.md"):
            try:
                content = file.read_text()
                # Parse frontmatter
                if content.startswith("---"):
                    end = content.find("---", 3)
                    if end > 0:
                        frontmatter = content[3:end]
                        title = ""
                        show = ""
                        date = ""
                        for line in frontmatter.strip().split("\n"):
                            if line.startswith('title:'):
                                title = line.split(":", 1)[1].strip().strip('"')
                            elif line.startswith('show:'):
                                show = line.split(":", 1)[1].strip().strip('"')
                            elif line.startswith('date:'):
                                date = line.split(":", 1)[1].strip()

                        results.append({
                            "title": title,
                            "show": show,
                            "date": date,
                            "filepath": str(file),
                            "relative_path": str(file.relative_to(self.vault_path)),
                            "mtime": file.stat().st_mtime,
                        })
            except Exception:
                continue

        # Sort by modification time (most recent first)
        results.sort(key=lambda x: x["mtime"], reverse=True)
        return results[:limit]

    def get_podcast_summary(self, filepath: str) -> Optional[str]:
        """Get the full email content from a podcast file.

        Returns everything between ## Summary and ## Full Transcript,
        formatted for Telegram display.
        """
        file = Path(filepath)
        if not file.exists():
            return None

        content = file.read_text()

        # Extract the content between ## Summary and ## Full Transcript
        summary_start = content.find("## Summary")
        if summary_start == -1:
            return None

        summary_start = summary_start + len("## Summary")
        summary_end = content.find("## Full Transcript")
        if summary_end == -1:
            summary_end = len(content)

        email_content = content[summary_start:summary_end].strip()

        # Format markdown headers for Telegram (## Header -> **Header**)
        # Telegram doesn't support ## headers
        lines = email_content.split('\n')
        formatted_lines = []
        for line in lines:
            if line.startswith('## '):
                # Convert ## Header to **Header**
                formatted_lines.append(f"\n**{line[3:]}**\n")
            elif line.startswith('# '):
                # Convert # Header to **Header**
                formatted_lines.append(f"\n**{line[2:]}**\n")
            else:
                formatted_lines.append(line)

        return '\n'.join(formatted_lines).strip()

    def get_podcast_transcript(self, filepath: str) -> Optional[str]:
        """Get the transcript from a podcast file."""
        file = Path(filepath)
        if not file.exists():
            return None

        content = file.read_text()

        # Extract transcript from the collapsible section
        transcript_marker = "<summary>Click to expand transcript</summary>"
        start = content.find(transcript_marker)
        if start == -1:
            return None

        start = start + len(transcript_marker)
        end = content.find("</details>", start)
        if end == -1:
            return None

        return content[start:end].strip()

    def update_podcast_summary(self, filepath: str, new_summary: str) -> bool:
        """Update the summary section of a podcast file.

        Returns True if successful.
        """
        file = Path(filepath)
        if not file.exists():
            return False

        content = file.read_text()

        # Find and replace the summary section
        summary_start = content.find("## Summary")
        if summary_start == -1:
            return False

        summary_content_start = summary_start + len("## Summary\n\n")
        summary_end = content.find("\n\n## Full Transcript")
        if summary_end == -1:
            summary_end = content.find("\n## Full Transcript")
        if summary_end == -1:
            return False

        # Rebuild the content
        new_content = (
            content[:summary_content_start] +
            new_summary +
            content[summary_end:]
        )

        file.write_text(new_content)
        return True

    def delete_podcast(self, filepath: str) -> bool:
        """Delete a podcast file from the vault.

        Returns True if successful.
        """
        file = Path(filepath)
        if not file.exists():
            return False

        try:
            file.unlink()
            return True
        except Exception:
            return False
