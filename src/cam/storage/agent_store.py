"""Agent storage and retrieval."""

from __future__ import annotations

import json
import sqlite3
from typing import TYPE_CHECKING

from cam.core.models import Agent, AgentEvent, AgentState, AgentStatus, TransportType

if TYPE_CHECKING:
    from cam.storage.database import Database


class AgentStore:
    """Manages storage and retrieval of agents and their events."""

    def __init__(self, db: Database):
        """Initialize agent store.

        Args:
            db: Database instance.
        """
        self.db = db

    def save(self, agent: Agent) -> None:
        """Save an agent (insert or update).

        Args:
            agent: Agent to save.

        Raises:
            AgentStoreError: If save operation fails.
        """
        try:
            self.db.execute(
                """
                INSERT INTO agents (
                    id, task_json, context_id, context_name, context_path,
                    transport_type, status, state, tmux_session, tmux_socket,
                    pid, started_at, completed_at, exit_reason, retry_count,
                    cost_estimate, files_changed, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    task_json = excluded.task_json,
                    context_id = excluded.context_id,
                    context_name = excluded.context_name,
                    context_path = excluded.context_path,
                    transport_type = excluded.transport_type,
                    status = excluded.status,
                    state = excluded.state,
                    tmux_session = excluded.tmux_session,
                    tmux_socket = excluded.tmux_socket,
                    pid = excluded.pid,
                    started_at = excluded.started_at,
                    completed_at = excluded.completed_at,
                    exit_reason = excluded.exit_reason,
                    retry_count = excluded.retry_count,
                    cost_estimate = excluded.cost_estimate,
                    files_changed = excluded.files_changed
                """,
                (
                    agent.id,
                    json.dumps(agent.task.model_dump(mode="json")),
                    agent.context_id,
                    agent.context_name,
                    str(agent.context_path),
                    agent.transport_type.value,
                    agent.status.value,
                    agent.state.value,
                    agent.tmux_session,
                    agent.tmux_socket,
                    agent.pid,
                    str(agent.started_at) if agent.started_at else None,
                    str(agent.completed_at) if agent.completed_at else None,
                    agent.exit_reason,
                    agent.retry_count,
                    agent.cost_estimate,
                    json.dumps(agent.files_changed),
                    str(agent.started_at) if agent.started_at else None,
                ),
            )
        except sqlite3.Error as e:
            raise AgentStoreError(f"Failed to save agent: {e}") from e

    def get(self, agent_id: str) -> Agent | None:
        """Get an agent by full or prefix ID.

        Supports short IDs: if *agent_id* is shorter than a full UUID the
        method looks for rows whose ``id`` column starts with the given
        prefix.  If multiple rows match, the most recently created one is
        returned.

        Args:
            agent_id: Full UUID or unique prefix (e.g. "86a9d46b").

        Returns:
            Agent if found, None otherwise.
        """
        # Try exact match first (fast path)
        row = self.db.fetchone("SELECT * FROM agents WHERE id = ?", (agent_id,))
        if row:
            return self._row_to_agent(row)

        # Fall back to prefix match
        row = self.db.fetchone(
            "SELECT * FROM agents WHERE id LIKE ? ORDER BY created_at DESC LIMIT 1",
            (agent_id + "%",),
        )
        return self._row_to_agent(row) if row else None

    def list(
        self,
        status: AgentStatus | None = None,
        context_id: str | None = None,
        tool: str | None = None,
        limit: int | None = None,
    ) -> list[Agent]:
        """List agents with optional filtering.

        Args:
            status: Filter by status.
            context_id: Filter by context ID.
            tool: Filter by tool name (searches in task.tool field).
            limit: Maximum number of agents to return.

        Returns:
            List of agents matching the filters.
        """
        query = "SELECT * FROM agents WHERE 1=1"
        params: list[str | int] = []

        if status:
            query += " AND status = ?"
            params.append(status.value)

        if context_id:
            query += " AND context_id = ?"
            params.append(context_id)

        # For tool filtering, we need to check the JSON field
        # SQLite JSON functions are available in recent versions
        if tool:
            query += " AND json_extract(task_json, '$.tool') = ?"
            params.append(tool)

        # Order by created_at descending (most recent first)
        query += " ORDER BY created_at DESC"

        if limit:
            query += " LIMIT ?"
            params.append(limit)

        rows = self.db.fetchall(query, tuple(params))
        return [self._row_to_agent(row) for row in rows]

    def update_status(
        self,
        agent_id: str,
        status: AgentStatus,
        state: AgentState | None = None,
        exit_reason: str | None = None,
    ) -> None:
        """Update agent status and optionally state and exit reason.

        Args:
            agent_id: Agent ID.
            status: New status.
            state: New state (optional).
            exit_reason: Exit reason (optional).

        Raises:
            AgentStoreError: If agent not found or update fails.
        """
        try:
            query = "UPDATE agents SET status = ?"
            params: list[str] = [status.value]

            if state:
                query += ", state = ?"
                params.append(state.value)

            if exit_reason is not None:
                query += ", exit_reason = ?"
                params.append(exit_reason)

            # Set completed_at if status is completed, failed, or cancelled
            if status in (AgentStatus.COMPLETED, AgentStatus.FAILED, AgentStatus.KILLED, AgentStatus.TIMEOUT):
                query += ", completed_at = datetime('now')"

            query += " WHERE id = ?"
            params.append(agent_id)

            cursor = self.db.execute(query, tuple(params))
            if cursor.rowcount == 0:
                raise AgentStoreError(f"Agent '{agent_id}' not found")
        except sqlite3.Error as e:
            raise AgentStoreError(f"Failed to update agent status: {e}") from e

    def add_event(self, event: AgentEvent) -> None:
        """Add an agent event.

        Args:
            event: Agent event to add.

        Raises:
            AgentStoreError: If event cannot be added.
        """
        try:
            self.db.execute(
                """
                INSERT INTO agent_events (agent_id, timestamp, event_type, detail)
                VALUES (?, ?, ?, ?)
                """,
                (
                    event.agent_id,
                    str(event.timestamp),
                    event.event_type,
                    json.dumps(event.detail),
                ),
            )
        except sqlite3.Error as e:
            raise AgentStoreError(f"Failed to add agent event: {e}") from e

    def get_events(self, agent_id: str) -> list[AgentEvent]:
        """Get all events for an agent.

        Args:
            agent_id: Agent ID.

        Returns:
            List of agent events, ordered by timestamp ascending.
        """
        rows = self.db.fetchall(
            """
            SELECT * FROM agent_events
            WHERE agent_id = ?
            ORDER BY timestamp ASC
            """,
            (agent_id,),
        )
        return [self._row_to_event(row) for row in rows]

    def list_ids_by_filter(
        self,
        statuses: list[str] | None = None,
        before: str | None = None,
        context_id: str | None = None,
    ) -> list[tuple[str, str | None]]:
        """List agent (id, tmux_session) pairs matching filter criteria.

        Args:
            statuses: Filter by status values (e.g. ["killed", "timeout"]).
            before: ISO datetime string — only agents started before this time.
            context_id: Filter by context ID.

        Returns:
            List of (agent_id, tmux_session) tuples.
        """
        query = "SELECT id, tmux_session FROM agents WHERE 1=1"
        params: list[str] = []

        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            query += f" AND status IN ({placeholders})"
            params.extend(statuses)

        if before:
            query += " AND started_at < ?"
            params.append(before)

        if context_id:
            query += " AND context_id = ?"
            params.append(context_id)

        rows = self.db.fetchall(query, tuple(params))
        return [(row["id"], row["tmux_session"]) for row in rows]

    def delete_batch(self, agent_ids: list[str]) -> int:
        """Delete multiple agents and their events in batch.

        Args:
            agent_ids: List of agent IDs to delete.

        Returns:
            Number of agents deleted.
        """
        if not agent_ids:
            return 0
        try:
            placeholders = ",".join("?" for _ in agent_ids)
            self.db.execute(
                f"DELETE FROM agent_events WHERE agent_id IN ({placeholders})",
                tuple(agent_ids),
            )
            cursor = self.db.execute(
                f"DELETE FROM agents WHERE id IN ({placeholders})",
                tuple(agent_ids),
            )
            return cursor.rowcount
        except sqlite3.Error as e:
            raise AgentStoreError(f"Failed to delete agents: {e}") from e

    def all_ids(self) -> set[str]:
        """Return all agent IDs in the database."""
        rows = self.db.fetchall("SELECT id FROM agents")
        return {row["id"] for row in rows}

    def all_session_names(self) -> set[str]:
        """Return all tmux session names in the database."""
        rows = self.db.fetchall(
            "SELECT tmux_session FROM agents WHERE tmux_session IS NOT NULL"
        )
        return {row["tmux_session"] for row in rows}

    def delete(self, agent_id: str) -> bool:
        """Delete an agent and its events.

        Args:
            agent_id: Agent ID.

        Returns:
            True if agent was deleted, False if not found.

        Raises:
            AgentStoreError: If deletion fails.
        """
        try:
            # Delete events first (foreign key constraint)
            self.db.execute("DELETE FROM agent_events WHERE agent_id = ?", (agent_id,))

            # Delete agent
            cursor = self.db.execute("DELETE FROM agents WHERE id = ?", (agent_id,))
            return cursor.rowcount > 0
        except sqlite3.Error as e:
            raise AgentStoreError(f"Failed to delete agent: {e}") from e

    def _row_to_agent(self, row: sqlite3.Row) -> Agent:
        """Convert a database row to an Agent object.

        Args:
            row: Database row.

        Returns:
            Agent object.
        """
        from cam.core.models import TaskDefinition

        task_dict = json.loads(row["task_json"])
        files_changed = json.loads(row["files_changed"])

        return Agent(
            id=row["id"],
            task=TaskDefinition(**task_dict),
            context_id=row["context_id"],
            context_name=row["context_name"],
            context_path=row["context_path"],
            transport_type=TransportType(row["transport_type"]),
            status=AgentStatus(row["status"]),
            state=AgentState(row["state"]),
            tmux_session=row["tmux_session"],
            tmux_socket=row["tmux_socket"],
            pid=row["pid"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            exit_reason=row["exit_reason"],
            retry_count=row["retry_count"],
            cost_estimate=row["cost_estimate"],
            files_changed=files_changed,
        )

    def _row_to_event(self, row: sqlite3.Row) -> AgentEvent:
        """Convert a database row to an AgentEvent object.

        Args:
            row: Database row.

        Returns:
            AgentEvent object.
        """
        detail = json.loads(row["detail"])

        return AgentEvent(
            agent_id=row["agent_id"],
            timestamp=row["timestamp"],
            event_type=row["event_type"],
            detail=detail,
        )


class AgentStoreError(Exception):
    """Agent store operation error."""

    pass
