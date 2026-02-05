"""Simple JSON-based storage for podcast summaries."""

import json
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional


@dataclass
class PodcastSummary:
    """A saved podcast summary."""
    id: str
    title: str
    show: Optional[str]
    email_content: str
    transcript: str
    url: Optional[str]
    duration: Optional[str]
    created_at: str
    updated_at: str
    categories: list[str] = None

    def __post_init__(self):
        if self.categories is None:
            self.categories = []


class SummaryStorage:
    """Simple JSON-based storage for podcast summaries."""

    def __init__(self, storage_path: Path | str):
        self.storage_path = Path(storage_path)
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        self._summaries: dict[str, PodcastSummary] = {}
        self._load()

    def _load(self) -> None:
        """Load summaries from disk."""
        if self.storage_path.exists():
            try:
                with open(self.storage_path) as f:
                    data = json.load(f)
                    for item in data:
                        # Backward compat: add categories if missing
                        if "categories" not in item:
                            item["categories"] = []
                        summary = PodcastSummary(**item)
                        self._summaries[summary.id] = summary
            except (json.JSONDecodeError, KeyError):
                self._summaries = {}

    def _save(self) -> None:
        """Save summaries to disk."""
        data = [asdict(s) for s in self._summaries.values()]
        with open(self.storage_path, 'w') as f:
            json.dump(data, f, indent=2)

    def save_summary(
        self,
        title: str,
        email_content: str,
        transcript: str,
        show: Optional[str] = None,
        url: Optional[str] = None,
        duration: Optional[str] = None,
    ) -> str:
        """Save a new summary. Returns the summary ID."""
        summary_id = str(uuid.uuid4())[:8]
        now = datetime.now().isoformat()

        summary = PodcastSummary(
            id=summary_id,
            title=title,
            show=show,
            email_content=email_content,
            transcript=transcript,
            url=url,
            duration=duration,
            created_at=now,
            updated_at=now,
            categories=[],
        )

        self._summaries[summary_id] = summary
        self._save()
        return summary_id

    def update_summary(self, summary_id: str, email_content: str) -> bool:
        """Update an existing summary's email content."""
        if summary_id not in self._summaries:
            return False

        summary = self._summaries[summary_id]
        self._summaries[summary_id] = PodcastSummary(
            id=summary.id,
            title=summary.title,
            show=summary.show,
            email_content=email_content,
            transcript=summary.transcript,
            url=summary.url,
            duration=summary.duration,
            created_at=summary.created_at,
            updated_at=datetime.now().isoformat(),
            categories=summary.categories,
        )
        self._save()
        return True

    def update_categories(self, summary_id: str, category_ids: list[str]) -> bool:
        """Update the categories list for a summary."""
        if summary_id not in self._summaries:
            return False

        self._summaries[summary_id].categories = category_ids
        self._summaries[summary_id].updated_at = datetime.now().isoformat()
        self._save()
        return True

    def list_all_ids(self) -> list[str]:
        """Get all summary IDs."""
        return list(self._summaries.keys())

    def delete_summary(self, summary_id: str) -> bool:
        """Delete a summary."""
        if summary_id not in self._summaries:
            return False

        del self._summaries[summary_id]
        self._save()
        return True

    def get_summary(self, summary_id: str) -> Optional[PodcastSummary]:
        """Get a summary by ID."""
        return self._summaries.get(summary_id)

    def list_summaries(self, limit: int = 10) -> list[PodcastSummary]:
        """Get recent summaries, sorted by creation date (newest first)."""
        summaries = sorted(
            self._summaries.values(),
            key=lambda s: s.created_at,
            reverse=True
        )
        return summaries[:limit]

    def count(self) -> int:
        """Get the number of stored summaries."""
        return len(self._summaries)
