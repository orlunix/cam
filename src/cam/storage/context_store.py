"""Context storage and retrieval."""

from __future__ import annotations

import json
import sqlite3
from typing import TYPE_CHECKING

from cam.core.models import Context, MachineConfig, TransportType

if TYPE_CHECKING:
    from cam.storage.database import Database


class ContextStore:
    """Manages storage and retrieval of contexts."""

    def __init__(self, db: Database):
        self.db = db

    def add(self, context: Context) -> None:
        """Add a new context."""
        try:
            self.db.execute(
                """
                INSERT INTO contexts (id, name, path, machine_config, tags, created_at, last_used_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(context.id),
                    context.name,
                    str(context.path),
                    json.dumps(context.machine.model_dump(mode="json")),
                    json.dumps(context.tags),
                    str(context.created_at) if context.created_at else None,
                    str(context.last_used_at) if context.last_used_at else None,
                ),
            )
        except sqlite3.IntegrityError as e:
            raise ContextStoreError(f"Context '{context.name}' already exists") from e
        except sqlite3.Error as e:
            raise ContextStoreError(f"Failed to add context: {e}") from e

    def get(self, name_or_id: str) -> Context | None:
        """Get a context by name or ID."""
        row = self.db.fetchone("SELECT * FROM contexts WHERE id = ?", (name_or_id,))
        if not row:
            row = self.db.fetchone("SELECT * FROM contexts WHERE name = ?", (name_or_id,))
        return self._row_to_context(row) if row else None

    def list(
        self, tags: list[str] | None = None, transport_type: TransportType | None = None
    ) -> list[Context]:
        """List contexts with optional filtering."""
        rows = self.db.fetchall("SELECT * FROM contexts ORDER BY created_at DESC")
        contexts = [self._row_to_context(row) for row in rows]

        if tags:
            contexts = [
                ctx for ctx in contexts if all(tag in ctx.tags for tag in tags)
            ]

        if transport_type:
            contexts = [
                ctx for ctx in contexts if ctx.machine.type == transport_type
            ]

        return contexts

    def update_last_used(self, context_id: str) -> None:
        """Update last_used_at timestamp."""
        try:
            cursor = self.db.execute(
                "UPDATE contexts SET last_used_at = datetime('now') WHERE id = ?",
                (context_id,),
            )
            if cursor.rowcount == 0:
                raise ContextStoreError(f"Context '{context_id}' not found")
        except sqlite3.Error as e:
            raise ContextStoreError(f"Failed to update context: {e}") from e

    def remove(self, name_or_id: str) -> bool:
        """Remove a context by name or ID."""
        try:
            cursor = self.db.execute("DELETE FROM contexts WHERE id = ?", (name_or_id,))
            if cursor.rowcount > 0:
                return True
            cursor = self.db.execute("DELETE FROM contexts WHERE name = ?", (name_or_id,))
            return cursor.rowcount > 0
        except sqlite3.Error as e:
            raise ContextStoreError(f"Failed to remove context: {e}") from e

    def exists(self, name: str) -> bool:
        """Check if a context with the given name exists."""
        row = self.db.fetchone("SELECT 1 FROM contexts WHERE name = ?", (name,))
        return row is not None

    def _row_to_context(self, row: sqlite3.Row) -> Context:
        """Convert a database row to a Context object."""
        machine_dict = json.loads(row["machine_config"])
        tags = json.loads(row["tags"])

        return Context(
            id=row["id"],
            name=row["name"],
            path=row["path"],
            machine=MachineConfig(**machine_dict),
            tags=tags,
            created_at=row["created_at"],
            last_used_at=row["last_used_at"],
        )


class ContextStoreError(Exception):
    """Context store operation error."""
    pass
