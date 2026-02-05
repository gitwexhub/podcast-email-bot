"""Learning system that improves summaries based on user feedback."""

import json
from dataclasses import dataclass, asdict, field
from datetime import datetime
from pathlib import Path
from typing import Optional
import logging

logger = logging.getLogger(__name__)


@dataclass
class FeedbackEntry:
    """A single piece of feedback from the user."""
    timestamp: str
    podcast_title: str
    feedback_text: str
    feedback_type: str  # "edit", "approve", "reject"
    topics: list[str] = field(default_factory=list)


@dataclass
class UserPreferences:
    """Learned preferences from user interactions."""
    # Summary style preferences
    preferred_length: str = "medium"  # "brief", "medium", "detailed"
    detail_level: str = "balanced"  # "high-level", "balanced", "granular"

    # Content preferences
    favorite_topics: list[str] = field(default_factory=list)
    avoid_topics: list[str] = field(default_factory=list)

    # Format preferences
    include_timestamps: bool = True
    include_soundbites: bool = True
    include_actionable_takeaways: bool = True

    # Tone preferences
    tone: str = "professional"  # "casual", "professional", "academic"

    # Learned patterns from feedback
    common_feedback_patterns: list[str] = field(default_factory=list)

    # Email preference
    default_email: str = ""

    # Stats
    total_podcasts_processed: int = 0
    total_edits_requested: int = 0
    total_approved_first_try: int = 0


class LearningSystem:
    """Learns from user feedback to improve future summaries."""

    def __init__(self, storage_path: Path | str):
        self.storage_path = Path(storage_path)
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        self.feedback_history: list[FeedbackEntry] = []
        self.preferences = UserPreferences()
        self._load()

    def _load(self) -> None:
        """Load learning data from disk."""
        if self.storage_path.exists():
            try:
                with open(self.storage_path) as f:
                    data = json.load(f)

                # Load feedback history
                for entry in data.get("feedback_history", []):
                    self.feedback_history.append(FeedbackEntry(**entry))

                # Load preferences
                prefs_data = data.get("preferences", {})
                self.preferences = UserPreferences(**prefs_data)

            except (json.JSONDecodeError, KeyError, TypeError) as e:
                logger.warning(f"Error loading learning data: {e}")
                self.feedback_history = []
                self.preferences = UserPreferences()

    def _save(self) -> None:
        """Save learning data to disk."""
        data = {
            "feedback_history": [asdict(f) for f in self.feedback_history],
            "preferences": asdict(self.preferences),
            "last_updated": datetime.now().isoformat(),
        }
        with open(self.storage_path, 'w') as f:
            json.dump(data, f, indent=2)

    def record_feedback(
        self,
        podcast_title: str,
        feedback_text: str,
        feedback_type: str,
        topics: list[str] = None,
    ) -> None:
        """Record user feedback and update preferences."""
        entry = FeedbackEntry(
            timestamp=datetime.now().isoformat(),
            podcast_title=podcast_title,
            feedback_text=feedback_text,
            feedback_type=feedback_type,
            topics=topics or [],
        )
        self.feedback_history.append(entry)

        # Update stats
        self.preferences.total_podcasts_processed += 1
        if feedback_type == "edit":
            self.preferences.total_edits_requested += 1
        elif feedback_type == "approve":
            self.preferences.total_approved_first_try += 1

        # Analyze feedback to learn preferences
        self._analyze_feedback(feedback_text, feedback_type)

        # Keep only last 100 feedback entries
        if len(self.feedback_history) > 100:
            self.feedback_history = self.feedback_history[-100:]

        self._save()

    def _analyze_feedback(self, feedback_text: str, feedback_type: str) -> None:
        """Analyze feedback to learn user preferences."""
        feedback_lower = feedback_text.lower()

        # Length preferences
        if any(word in feedback_lower for word in ["shorter", "brief", "concise", "too long"]):
            self._adjust_length_preference("brief")
        elif any(word in feedback_lower for word in ["longer", "more detail", "elaborate", "too short"]):
            self._adjust_length_preference("detailed")

        # Detail level
        if any(word in feedback_lower for word in ["high-level", "overview", "summary only"]):
            self.preferences.detail_level = "high-level"
        elif any(word in feedback_lower for word in ["granular", "specific", "in-depth", "deep dive"]):
            self.preferences.detail_level = "granular"

        # Tone preferences
        if any(word in feedback_lower for word in ["casual", "informal", "conversational"]):
            self.preferences.tone = "casual"
        elif any(word in feedback_lower for word in ["formal", "professional", "business"]):
            self.preferences.tone = "professional"
        elif any(word in feedback_lower for word in ["academic", "scholarly", "technical"]):
            self.preferences.tone = "academic"

        # Feature preferences
        if "no timestamp" in feedback_lower or "remove timestamp" in feedback_lower:
            self.preferences.include_timestamps = False
        if "add timestamp" in feedback_lower or "include timestamp" in feedback_lower:
            self.preferences.include_timestamps = True

        if "no soundbite" in feedback_lower or "remove quote" in feedback_lower:
            self.preferences.include_soundbites = False
        if "add soundbite" in feedback_lower or "more quote" in feedback_lower:
            self.preferences.include_soundbites = True

        # Store common feedback patterns (for the prompt)
        if feedback_type == "edit" and len(feedback_text) > 10:
            # Extract key phrases from feedback
            pattern = self._extract_pattern(feedback_text)
            if pattern and pattern not in self.preferences.common_feedback_patterns:
                self.preferences.common_feedback_patterns.append(pattern)
                # Keep only last 10 patterns
                if len(self.preferences.common_feedback_patterns) > 10:
                    self.preferences.common_feedback_patterns = self.preferences.common_feedback_patterns[-10:]

    def _adjust_length_preference(self, direction: str) -> None:
        """Gradually adjust length preference."""
        levels = ["brief", "medium", "detailed"]
        current_idx = levels.index(self.preferences.preferred_length)

        if direction == "brief" and current_idx > 0:
            self.preferences.preferred_length = levels[current_idx - 1]
        elif direction == "detailed" and current_idx < 2:
            self.preferences.preferred_length = levels[current_idx + 1]

    def _extract_pattern(self, feedback: str) -> Optional[str]:
        """Extract a reusable pattern from feedback."""
        # Remove very generic phrases
        generic = ["make it", "can you", "please", "i want", "i'd like"]
        pattern = feedback.lower()
        for g in generic:
            pattern = pattern.replace(g, "")
        pattern = pattern.strip()

        # Only keep if meaningful
        if len(pattern) > 5 and len(pattern) < 100:
            return pattern
        return None

    def record_topic_interest(self, topics: list[str]) -> None:
        """Record topics the user is interested in."""
        for topic in topics:
            topic_lower = topic.lower()
            if topic_lower not in [t.lower() for t in self.preferences.favorite_topics]:
                self.preferences.favorite_topics.append(topic)

        # Keep only top 20 topics
        if len(self.preferences.favorite_topics) > 20:
            self.preferences.favorite_topics = self.preferences.favorite_topics[-20:]

        self._save()

    def get_prompt_context(self) -> str:
        """Generate context to inject into the summarizer prompt."""
        context_parts = []

        # Length and detail preferences
        length_guidance = {
            "brief": "Keep the summary concise and focused on key points only.",
            "medium": "Provide a balanced summary with appropriate detail.",
            "detailed": "Provide a comprehensive summary with thorough detail.",
        }
        context_parts.append(length_guidance.get(self.preferences.preferred_length, ""))

        detail_guidance = {
            "high-level": "Focus on the big picture and main themes.",
            "balanced": "Balance high-level themes with specific examples.",
            "granular": "Include specific details, examples, and nuances.",
        }
        context_parts.append(detail_guidance.get(self.preferences.detail_level, ""))

        # Tone
        tone_guidance = {
            "casual": "Use a conversational, approachable tone.",
            "professional": "Use a professional, business-appropriate tone.",
            "academic": "Use a formal, analytical tone with precise language.",
        }
        context_parts.append(tone_guidance.get(self.preferences.tone, ""))

        # Feature preferences
        if not self.preferences.include_timestamps:
            context_parts.append("Do not include timestamps in the summary.")
        if not self.preferences.include_soundbites:
            context_parts.append("Do not include direct quotes or soundbites.")
        if not self.preferences.include_actionable_takeaways:
            context_parts.append("Focus on information rather than actionable takeaways.")

        # Common feedback patterns (most valuable - direct user preferences)
        if self.preferences.common_feedback_patterns:
            patterns = self.preferences.common_feedback_patterns[-5:]  # Last 5
            context_parts.append(
                f"Based on past feedback, the user prefers: {'; '.join(patterns)}"
            )

        # Topic interests
        if self.preferences.favorite_topics:
            topics = self.preferences.favorite_topics[-10:]  # Last 10
            context_parts.append(
                f"The user is particularly interested in these topics when they appear: {', '.join(topics)}"
            )

        # Filter empty parts and join
        context = "\n".join(p for p in context_parts if p)

        if context:
            return f"\n\n## User Preferences (learned from past interactions):\n{context}\n"
        return ""

    def get_stats(self) -> dict:
        """Get learning statistics."""
        approval_rate = 0
        if self.preferences.total_podcasts_processed > 0:
            approval_rate = (
                self.preferences.total_approved_first_try /
                self.preferences.total_podcasts_processed * 100
            )

        return {
            "total_processed": self.preferences.total_podcasts_processed,
            "approved_first_try": self.preferences.total_approved_first_try,
            "edits_requested": self.preferences.total_edits_requested,
            "approval_rate": f"{approval_rate:.1f}%",
            "preferred_length": self.preferences.preferred_length,
            "preferred_tone": self.preferences.tone,
            "topics_of_interest": len(self.preferences.favorite_topics),
            "learned_patterns": len(self.preferences.common_feedback_patterns),
        }

    def reset_preferences(self) -> None:
        """Reset all learned preferences (keep history)."""
        self.preferences = UserPreferences()
        self._save()

    def set_default_email(self, email: str) -> None:
        """Set the default email address for sending summaries."""
        self.preferences.default_email = email
        self._save()

    def get_default_email(self) -> str:
        """Get the stored default email address."""
        return self.preferences.default_email
