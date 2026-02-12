"""SQLite database management with auto-creation and migrations."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from cam.constants import DB_PATH

# Current schema version
SCHEMA_VERSION = 1


class Database:
    """Manages SQLite database connection and schema migrations."""

    def __init__(self, db_path: Path | None = None):
        """Initialize database connection.

        Args:
            db_path: Path to SQLite database file. Defaults to cam.constants.DB_PATH.
        """
        self.db_path = db_path or DB_PATH

        # Create parent directories if needed
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        # Connect with check_same_thread=False for multi-threaded access
        self.conn = sqlite3.connect(
            str(self.db_path),
            check_same_thread=False,
            isolation_level=None,  # Autocommit mode
        )
        self.conn.row_factory = sqlite3.Row

        # Enable WAL mode for better concurrent reads
        self.conn.execute("PRAGMA journal_mode=WAL")

        # Run migrations
        self._migrate()

    def _migrate(self) -> None:
        """Apply database migrations up to current schema version."""
        # Create schema_version table if it doesn't exist
        self.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_version (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )

        # Get current version
        row = self.fetchone("SELECT MAX(version) as version FROM schema_version")
        current_version = row["version"] if row and row["version"] else 0

        # Apply migrations
        if current_version < 1:
            self._migrate_to_v1()

    def _migrate_to_v1(self) -> None:
        """Migrate to schema version 1."""
        # Create contexts table
        self.execute(
            """
            CREATE TABLE IF NOT EXISTS contexts (
                id TEXT PRIMARY KEY,
                name TEXT UNIQUE NOT NULL,
                path TEXT NOT NULL,
                machine_config TEXT NOT NULL,
                tags TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                last_used_at TEXT
            )
            """
        )

        # Create agents table
        self.execute(
            """
            CREATE TABLE IF NOT EXISTS agents (
                id TEXT PRIMARY KEY,
                task_json TEXT NOT NULL,
                context_id TEXT NOT NULL,
                context_name TEXT NOT NULL,
                context_path TEXT NOT NULL,
                transport_type TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                state TEXT NOT NULL DEFAULT 'initializing',
                tmux_session TEXT,
                tmux_socket TEXT,
                pid INTEGER,
                started_at TEXT,
                completed_at TEXT,
                exit_reason TEXT,
                retry_count INTEGER NOT NULL DEFAULT 0,
                cost_estimate REAL,
                files_changed TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )

        # Create agent_events table
        self.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_id TEXT NOT NULL,
                timestamp TEXT NOT NULL DEFAULT (datetime('now')),
                event_type TEXT NOT NULL,
                detail TEXT NOT NULL DEFAULT '{}'
            )
            """
        )

        # Create indexes
        self.execute("CREATE INDEX IF NOT EXISTS idx_agents_status ON agents(status)")
        self.execute("CREATE INDEX IF NOT EXISTS idx_agents_context_id ON agents(context_id)")
        self.execute("CREATE INDEX IF NOT EXISTS idx_agent_events_agent_id ON agent_events(agent_id)")

        # Record migration
        self.execute("INSERT INTO schema_version (version) VALUES (1)")

    def execute(self, sql: str, params: tuple[Any, ...] | dict[str, Any] = ()) -> sqlite3.Cursor:
        """Execute a SQL statement.

        Args:
            sql: SQL statement to execute.
            params: Parameters for the SQL statement.

        Returns:
            Cursor object.
        """
        try:
            return self.conn.execute(sql, params)
        except sqlite3.Error as e:
            raise DatabaseError(f"Failed to execute SQL: {e}") from e

    def executemany(
        self, sql: str, params_list: list[tuple[Any, ...] | dict[str, Any]]
    ) -> sqlite3.Cursor:
        """Execute a SQL statement multiple times.

        Args:
            sql: SQL statement to execute.
            params_list: List of parameters for each execution.

        Returns:
            Cursor object.
        """
        try:
            return self.conn.executemany(sql, params_list)
        except sqlite3.Error as e:
            raise DatabaseError(f"Failed to execute SQL: {e}") from e

    def fetchone(
        self, sql: str, params: tuple[Any, ...] | dict[str, Any] = ()
    ) -> sqlite3.Row | None:
        """Fetch a single row.

        Args:
            sql: SQL query to execute.
            params: Parameters for the SQL query.

        Returns:
            Single row or None if no results.
        """
        cursor = self.execute(sql, params)
        return cursor.fetchone()

    def fetchall(
        self, sql: str, params: tuple[Any, ...] | dict[str, Any] = ()
    ) -> list[sqlite3.Row]:
        """Fetch all rows.

        Args:
            sql: SQL query to execute.
            params: Parameters for the SQL query.

        Returns:
            List of rows.
        """
        cursor = self.execute(sql, params)
        return cursor.fetchall()

    def close(self) -> None:
        """Close database connection."""
        if self.conn:
            self.conn.close()

    def __enter__(self) -> Database:
        """Context manager entry."""
        return self

    def __exit__(self, exc_type: type, exc_val: Exception, exc_tb: Any) -> None:
        """Context manager exit."""
        self.close()


class DatabaseError(Exception):
    """Database operation error."""

    pass
