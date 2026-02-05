"""Hierarchical folder/category storage for podcast summaries."""

import json
import uuid
from dataclasses import dataclass, asdict, field
from datetime import datetime
from pathlib import Path
from typing import Optional


@dataclass
class Category:
    """A folder/category for organizing podcast summaries."""
    id: str
    name: str
    emoji: str
    description: str
    parent_id: Optional[str]
    summary_ids: list[str]
    created_at: str
    updated_at: str


class CategoryStorage:
    """Hierarchical category storage with CRUD and tree operations."""

    def __init__(self, storage_path: Path | str):
        self.storage_path = Path(storage_path)
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        self._categories: dict[str, Category] = {}
        self._save_count: int = 0
        self._load()

    def _load(self) -> None:
        """Load categories from disk."""
        if self.storage_path.exists():
            try:
                with open(self.storage_path) as f:
                    data = json.load(f)
                    self._save_count = data.get("save_count", 0)
                    for item in data.get("categories", []):
                        cat = Category(**item)
                        self._categories[cat.id] = cat
            except (json.JSONDecodeError, KeyError, TypeError):
                self._categories = {}

    def _save(self) -> None:
        """Save categories to disk."""
        data = {
            "save_count": self._save_count,
            "categories": [asdict(c) for c in self._categories.values()],
        }
        with open(self.storage_path, 'w') as f:
            json.dump(data, f, indent=2)

    def create_category(
        self,
        name: str,
        emoji: str = "",
        description: str = "",
        parent_id: Optional[str] = None,
    ) -> str:
        """Create a new category. Returns the category ID."""
        # Validate parent exists if specified
        if parent_id and parent_id not in self._categories:
            raise ValueError(f"Parent category {parent_id} does not exist")

        # Cap hierarchy depth at 2 levels
        if parent_id:
            parent = self._categories[parent_id]
            if parent.parent_id is not None:
                raise ValueError("Maximum folder depth is 2 levels")

        cat_id = str(uuid.uuid4())[:8]
        now = datetime.now().isoformat()

        category = Category(
            id=cat_id,
            name=name,
            emoji=emoji,
            description=description,
            parent_id=parent_id,
            summary_ids=[],
            created_at=now,
            updated_at=now,
        )

        self._categories[cat_id] = category
        self._save()
        return cat_id

    def add_summary(self, summary_id: str, category_id: str) -> bool:
        """Add a summary to a category."""
        if category_id not in self._categories:
            return False

        cat = self._categories[category_id]
        if summary_id not in cat.summary_ids:
            cat.summary_ids.append(summary_id)
            cat.updated_at = datetime.now().isoformat()
            self._save()
        return True

    def remove_summary(self, summary_id: str, category_id: Optional[str] = None) -> bool:
        """Remove a summary from a specific category or all categories."""
        removed = False
        targets = [self._categories[category_id]] if category_id else self._categories.values()

        for cat in targets:
            if summary_id in cat.summary_ids:
                cat.summary_ids.remove(summary_id)
                cat.updated_at = datetime.now().isoformat()
                removed = True

        if removed:
            self._save()
        return removed

    def move_summary(self, summary_id: str, from_id: str, to_id: str) -> bool:
        """Move a summary from one category to another."""
        if from_id not in self._categories or to_id not in self._categories:
            return False

        from_cat = self._categories[from_id]
        to_cat = self._categories[to_id]

        if summary_id not in from_cat.summary_ids:
            return False

        from_cat.summary_ids.remove(summary_id)
        if summary_id not in to_cat.summary_ids:
            to_cat.summary_ids.append(summary_id)

        now = datetime.now().isoformat()
        from_cat.updated_at = now
        to_cat.updated_at = now
        self._save()
        return True

    def list_tree(self) -> list[dict]:
        """Return the full category tree as a nested structure.

        Returns list of top-level categories, each with a 'children' list.
        """
        tree = []
        # Get top-level categories
        roots = [c for c in self._categories.values() if c.parent_id is None]
        roots.sort(key=lambda c: c.name)

        for root in roots:
            children = self.get_children(root.id)
            children.sort(key=lambda c: c.name)
            tree.append({
                "id": root.id,
                "name": root.name,
                "emoji": root.emoji,
                "description": root.description,
                "count": len(root.summary_ids),
                "children": [
                    {
                        "id": child.id,
                        "name": child.name,
                        "emoji": child.emoji,
                        "description": child.description,
                        "count": len(child.summary_ids),
                    }
                    for child in children
                ],
            })

        return tree

    def list_root_categories(self) -> list[Category]:
        """Get top-level categories only."""
        roots = [c for c in self._categories.values() if c.parent_id is None]
        roots.sort(key=lambda c: c.name)
        return roots

    def get_children(self, category_id: str) -> list[Category]:
        """Get sub-folders of a category."""
        children = [c for c in self._categories.values() if c.parent_id == category_id]
        children.sort(key=lambda c: c.name)
        return children

    def get_category(self, category_id: str) -> Optional[Category]:
        """Get a single category by ID."""
        return self._categories.get(category_id)

    def rename_category(self, category_id: str, new_name: str, new_emoji: Optional[str] = None) -> bool:
        """Rename a category and optionally update its emoji."""
        if category_id not in self._categories:
            return False

        cat = self._categories[category_id]
        cat.name = new_name
        if new_emoji is not None:
            cat.emoji = new_emoji
        cat.updated_at = datetime.now().isoformat()
        self._save()
        return True

    def move_category(self, category_id: str, new_parent_id: Optional[str]) -> bool:
        """Re-parent a category. None = make top-level."""
        if category_id not in self._categories:
            return False

        if new_parent_id:
            if new_parent_id not in self._categories:
                return False
            # Prevent moving to own child
            if new_parent_id == category_id:
                return False
            # Prevent creating depth > 2
            parent = self._categories[new_parent_id]
            if parent.parent_id is not None:
                return False

        cat = self._categories[category_id]

        # If this category has children and is being moved under another, reject
        if new_parent_id and self.get_children(category_id):
            return False

        cat.parent_id = new_parent_id
        cat.updated_at = datetime.now().isoformat()
        self._save()
        return True

    def delete_category(self, category_id: str) -> list[str]:
        """Delete a category. Moves summaries to parent (or removes assignment).

        Returns list of orphaned summary IDs (those that lost their only category).
        """
        if category_id not in self._categories:
            return []

        cat = self._categories[category_id]
        orphaned = []

        # Move summaries to parent category if one exists
        if cat.parent_id and cat.parent_id in self._categories:
            parent = self._categories[cat.parent_id]
            for sid in cat.summary_ids:
                if sid not in parent.summary_ids:
                    parent.summary_ids.append(sid)
            parent.updated_at = datetime.now().isoformat()
        else:
            orphaned = list(cat.summary_ids)

        # Re-parent children to this category's parent
        for child in self.get_children(category_id):
            child.parent_id = cat.parent_id
            child.updated_at = datetime.now().isoformat()

        del self._categories[category_id]
        self._save()
        return orphaned

    def get_categories_for_summary(self, summary_id: str) -> list[Category]:
        """Get all categories containing a summary."""
        return [c for c in self._categories.values() if summary_id in c.summary_ids]

    def find_by_name(self, name: str) -> list[Category]:
        """Fuzzy match categories by name (case-insensitive substring)."""
        name_lower = name.lower().strip()
        matches = []
        for cat in self._categories.values():
            if name_lower in cat.name.lower():
                matches.append(cat)
        return matches

    def get_uncategorized_summaries(self, all_summary_ids: list[str]) -> list[str]:
        """Get summary IDs that aren't in any category."""
        categorized = set()
        for cat in self._categories.values():
            categorized.update(cat.summary_ids)
        return [sid for sid in all_summary_ids if sid not in categorized]

    def increment_save_count(self) -> int:
        """Increment and return the save counter (for triggering reorganization)."""
        self._save_count += 1
        self._save()
        return self._save_count

    def get_save_count(self) -> int:
        """Get the current save count."""
        return self._save_count

    def total_categories(self) -> int:
        """Get total number of categories."""
        return len(self._categories)

    def format_tree_display(self) -> str:
        """Format the category tree for Telegram display."""
        tree = self.list_tree()
        if not tree:
            return ""

        lines = []
        for root in tree:
            emoji = root["emoji"] or ""
            total = root["count"] + sum(c["count"] for c in root["children"])
            if total > 0:
                lines.append(f"{emoji} **{root['name']}** ({total})")
            else:
                lines.append(f"{emoji} **{root['name']}**")

            for child in root["children"]:
                c_emoji = child["emoji"] or ""
                if child["count"] > 0:
                    lines.append(f"  {c_emoji} {child['name']} ({child['count']})")
                else:
                    lines.append(f"  {c_emoji} {child['name']}")

        return "\n".join(lines)

    def get_flat_list(self) -> list[Category]:
        """Get all categories in a flat list, sorted: parents first, then children."""
        result = []
        for root in self.list_root_categories():
            result.append(root)
            result.extend(self.get_children(root.id))
        return result

    def export_to_markdown(self, vault_path: Path | str) -> str:
        """Export categories to a markdown file for backup.

        Returns the path to the exported file.
        """
        vault_path = Path(vault_path)
        export_path = vault_path / "content" / "podcasts" / "_library_index.md"
        export_path.parent.mkdir(parents=True, exist_ok=True)

        lines = [
            "---",
            "title: Podcast Library Index",
            "type: index",
            f"updated: {datetime.now().isoformat()}",
            "---",
            "",
            "# Podcast Library Index",
            "",
            "This file is auto-generated as a backup of your folder structure.",
            "",
            "## Folder Structure",
            "",
        ]

        tree = self.list_tree()
        for root in tree:
            lines.append(f"### {root['emoji']} {root['name']}")
            if root.get('description'):
                lines.append(f"_{root['description']}_")
            lines.append(f"- ID: `{root['id']}`")
            lines.append(f"- Podcasts: {root['count']}")
            lines.append("")

            for child in root.get("children", []):
                lines.append(f"#### {child['emoji']} {child['name']}")
                if child.get('description'):
                    lines.append(f"_{child['description']}_")
                lines.append(f"- ID: `{child['id']}`")
                lines.append(f"- Podcasts: {child['count']}")
                lines.append("")

        lines.append("## All Categories (JSON backup)")
        lines.append("")
        lines.append("```json")
        lines.append(json.dumps([asdict(c) for c in self._categories.values()], indent=2))
        lines.append("```")

        export_path.write_text("\n".join(lines))
        return str(export_path)

    def apply_reorganization(self, operations: list[dict]) -> list[str]:
        """Apply a batch of reorganization operations from AI.

        Operations format:
        - {"op": "merge", "source_id": "...", "target_id": "..."}
        - {"op": "create", "name": "...", "emoji": "...", "parent_id": "...", "summary_ids": [...]}
        - {"op": "move_summaries", "summary_ids": [...], "to_id": "..."}
        - {"op": "rename", "category_id": "...", "name": "...", "emoji": "..."}

        Returns list of human-readable change descriptions.
        """
        changes = []

        for op in operations:
            try:
                if op["op"] == "merge":
                    source = self._categories.get(op["source_id"])
                    target = self._categories.get(op["target_id"])
                    if source and target:
                        # Move all summaries from source to target
                        for sid in source.summary_ids:
                            if sid not in target.summary_ids:
                                target.summary_ids.append(sid)
                        target.updated_at = datetime.now().isoformat()
                        # Re-parent source's children to target
                        for child in self.get_children(source.id):
                            child.parent_id = target.id
                        del self._categories[source.id]
                        changes.append(f"Merged '{source.name}' into '{target.name}'")

                elif op["op"] == "create":
                    cat_id = self.create_category(
                        name=op["name"],
                        emoji=op.get("emoji", ""),
                        parent_id=op.get("parent_id"),
                    )
                    # Move specified summaries into new category
                    for sid in op.get("summary_ids", []):
                        self.add_summary(sid, cat_id)
                        # Remove from old location
                        for cat in self._categories.values():
                            if cat.id != cat_id and sid in cat.summary_ids:
                                cat.summary_ids.remove(sid)
                    changes.append(f"Created '{op['name']}' with {len(op.get('summary_ids', []))} items")

                elif op["op"] == "move_summaries":
                    target = self._categories.get(op["to_id"])
                    if target:
                        for sid in op.get("summary_ids", []):
                            if sid not in target.summary_ids:
                                target.summary_ids.append(sid)
                            # Remove from other categories
                            for cat in self._categories.values():
                                if cat.id != target.id and sid in cat.summary_ids:
                                    cat.summary_ids.remove(sid)
                        changes.append(f"Moved {len(op['summary_ids'])} items to '{target.name}'")

                elif op["op"] == "rename":
                    cat = self._categories.get(op["category_id"])
                    if cat:
                        old_name = cat.name
                        cat.name = op["name"]
                        if "emoji" in op:
                            cat.emoji = op["emoji"]
                        cat.updated_at = datetime.now().isoformat()
                        changes.append(f"Renamed '{old_name}' to '{op['name']}'")

            except (KeyError, ValueError) as e:
                continue

        self._save()
        return changes
