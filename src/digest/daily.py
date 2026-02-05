"""Daily digest generation and scheduling."""

import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from src.ai.summarizer import Summarizer
from src.config import Config
from src.storage.vault import VaultWriter
from src.storage.vectors import VectorStore

logger = logging.getLogger(__name__)


class DailyDigest:
    """Handles daily digest generation and delivery."""

    def __init__(
        self,
        config: Config,
        vault: VaultWriter,
        vector_store: VectorStore,
        send_telegram_message: Optional[Callable] = None,
    ):
        self.config = config
        self.vault = vault
        self.vector_store = vector_store
        self.send_telegram_message = send_telegram_message
        self._summarizer = None
        self._scheduler = None

    def _get_summarizer(self) -> Summarizer:
        """Lazy load summarizer."""
        if self._summarizer is None:
            self._summarizer = Summarizer(self.config)
        return self._summarizer

    def start_scheduler(self) -> None:
        """Start the daily digest scheduler."""
        if self._scheduler is not None:
            return

        self._scheduler = AsyncIOScheduler()

        # Parse configured time
        hour, minute = map(int, self.config.digest.time.split(":"))
        timezone = ZoneInfo(self.config.digest.timezone)

        trigger = CronTrigger(
            hour=hour,
            minute=minute,
            timezone=timezone,
        )

        self._scheduler.add_job(
            self.generate_and_send_digest,
            trigger,
            id="daily_digest",
            name="Generate daily knowledge digest",
        )

        self._scheduler.start()
        logger.info(
            f"Daily digest scheduled for {self.config.digest.time} "
            f"({self.config.digest.timezone})"
        )

    def stop_scheduler(self) -> None:
        """Stop the scheduler."""
        if self._scheduler:
            self._scheduler.shutdown()
            self._scheduler = None

    async def generate_and_send_digest(self, target_date: Optional[datetime] = None) -> str:
        """Generate and send the daily digest.

        Args:
            target_date: The date to generate digest for (defaults to today)

        Returns:
            Path to the saved digest file
        """
        if target_date is None:
            tz = ZoneInfo(self.config.digest.timezone)
            target_date = datetime.now(tz)

        date_str = target_date.strftime("%Y-%m-%d")
        logger.info(f"Generating daily digest for {date_str}")

        # Gather content from the day
        content_items = self._gather_days_content(target_date)

        if not content_items:
            logger.info(f"No content for {date_str}, skipping digest")
            return ""

        # Generate digest summary
        summarizer = self._get_summarizer()
        digest_result = await summarizer.generate_daily_digest(
            content_items=content_items,
            date_str=date_str,
        )

        # Save to vault
        vault_path = self.vault.save_daily_digest(
            date=target_date,
            summary=digest_result["summary"],
            content_processed=content_items,
            themes=digest_result.get("themes", []),
            connections=digest_result.get("connections", []),
        )

        # Send to Telegram if callback is configured
        if self.send_telegram_message:
            message = self._format_telegram_digest(
                date_str=date_str,
                summary=digest_result["summary"],
                content_items=content_items,
                themes=digest_result.get("themes", []),
            )
            await self.send_telegram_message(message)

        logger.info(f"Daily digest saved to {vault_path}")
        return vault_path

    def _gather_days_content(self, target_date: datetime) -> list[dict]:
        """Gather all content processed on a specific day."""
        # Get content from vector store for the day
        recent = self.vector_store.get_recent(days=1)

        # Filter to exact date
        date_str = target_date.strftime("%Y-%m-%d")
        content_items = []

        for item in recent:
            item_date = item.created_at.strftime("%Y-%m-%d")
            if item_date == date_str:
                content_items.append({
                    "type": item.content_type,
                    "title": item.title,
                    "summary": item.summary,
                    "path": item.vault_path,
                })

        return content_items

    def _format_telegram_digest(
        self,
        date_str: str,
        summary: str,
        content_items: list[dict],
        themes: list[str],
    ) -> str:
        """Format digest for Telegram delivery."""
        message_parts = [
            f"📚 **Daily Knowledge Digest - {date_str}**\n",
        ]

        # Content count summary
        type_counts = {}
        for item in content_items:
            t = item["type"]
            type_counts[t] = type_counts.get(t, 0) + 1

        counts_str = ", ".join(f"{count} {t}(s)" for t, count in type_counts.items())
        message_parts.append(f"Today you consumed: {counts_str}\n")

        # Summary
        message_parts.append(f"\n**Summary:**\n{summary}\n")

        # Themes
        if themes:
            message_parts.append("\n**Key Themes:**")
            for theme in themes[:5]:
                message_parts.append(f"• {theme}")

        # Content list
        message_parts.append("\n**Content Processed:**")
        for item in content_items[:10]:  # Limit to avoid very long messages
            emoji = {
                "podcast": "🎙️",
                "article": "📰",
                "thread": "🧵",
                "note": "📝",
                "insight": "💡",
            }.get(item["type"], "📄")
            message_parts.append(f"{emoji} {item['title']}")

        message_parts.append("\n_Check your Obsidian vault for the full digest with links._")

        return "\n".join(message_parts)

    async def generate_weekly_summary(self) -> str:
        """Generate a weekly summary of all content."""
        tz = ZoneInfo(self.config.digest.timezone)
        end_date = datetime.now(tz)
        start_date = end_date - timedelta(days=7)

        # Gather week's content
        recent = self.vector_store.get_recent(days=7)

        content_items = [
            {
                "type": item.content_type,
                "title": item.title,
                "summary": item.summary,
                "path": item.vault_path,
            }
            for item in recent
        ]

        if not content_items:
            return "No content processed this week."

        # Generate summary
        summarizer = self._get_summarizer()

        prompt_items = [
            f"**{item['type'].upper()}: {item['title']}**\n{item['summary'][:200]}..."
            for item in content_items
        ]

        # Use daily digest but frame it as weekly
        result = await summarizer.generate_daily_digest(
            content_items=content_items,
            date_str=f"Week of {start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}",
        )

        # Format for return
        summary = f"📊 **Weekly Knowledge Summary**\n\n"
        summary += f"Content consumed: {len(content_items)} items\n\n"
        summary += f"**Summary:**\n{result['summary']}\n\n"

        if result.get("themes"):
            summary += "**Key Themes:**\n"
            for theme in result["themes"]:
                summary += f"• {theme}\n"

        return summary


class DigestScheduler:
    """Manages the digest scheduling lifecycle."""

    def __init__(self, digest: DailyDigest):
        self.digest = digest

    def start(self) -> None:
        """Start the digest scheduler."""
        self.digest.start_scheduler()

    def stop(self) -> None:
        """Stop the digest scheduler."""
        self.digest.stop_scheduler()

    async def trigger_now(self) -> str:
        """Manually trigger a digest generation."""
        return await self.digest.generate_and_send_digest()

    async def generate_for_date(self, date: datetime) -> str:
        """Generate digest for a specific past date."""
        return await self.digest.generate_and_send_digest(target_date=date)
