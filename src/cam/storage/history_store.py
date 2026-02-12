"""History queries for completed agents.

Provides aggregated views and statistics over the agent history stored
in SQLite. All queries operate on the existing agents and agent_events
tables — no separate history table is needed.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from cam.core.models import Agent, AgentStatus
from cam.storage.agent_store import AgentStore


class HistoryStore:
    """Query layer for completed agent history and statistics.

    Built on top of AgentStore, providing filtered views and aggregations
    for the history and stats CLI commands.
    """

    def __init__(self, db: Any) -> None:
        self.db = db

    def list_history(
        self,
        context_name: str | None = None,
        tool: str | None = None,
        status: str | None = None,
        since: datetime | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Query completed agents with optional filters.

        Returns lightweight dicts for display, not full Agent models.
        """
        query = """
            SELECT id, task_json, context_name, transport_type,
                   status, started_at, completed_at, exit_reason,
                   cost_estimate, retry_count
            FROM agents
            WHERE 1=1
        """
        params: list[Any] = []

        if context_name:
            query += " AND context_name = ?"
            params.append(context_name)

        if tool:
            query += " AND json_extract(task_json, '$.tool') = ?"
            params.append(tool)

        if status:
            query += " AND status = ?"
            params.append(status)
        else:
            # Default: show terminal states
            query += " AND status IN ('completed', 'failed', 'timeout', 'killed')"

        if since:
            query += " AND started_at >= ?"
            params.append(str(since))

        query += " ORDER BY started_at DESC LIMIT ?"
        params.append(limit)

        rows = self.db.fetchall(query, tuple(params))

        results = []
        for row in rows:
            task = json.loads(row["task_json"])
            duration = None
            if row["started_at"] and row["completed_at"]:
                try:
                    start = datetime.fromisoformat(str(row["started_at"]))
                    end = datetime.fromisoformat(str(row["completed_at"]))
                    duration = (end - start).total_seconds()
                except (ValueError, TypeError):
                    pass

            results.append({
                "id": row["id"],
                "task_name": task.get("name", ""),
                "tool": task.get("tool", ""),
                "prompt": task.get("prompt", ""),
                "context": row["context_name"],
                "status": row["status"],
                "started_at": row["started_at"],
                "completed_at": row["completed_at"],
                "duration": duration,
                "exit_reason": row["exit_reason"],
                "cost_estimate": row["cost_estimate"],
                "retry_count": row["retry_count"],
            })

        return results

    def get_stats(
        self,
        context_name: str | None = None,
        since: datetime | None = None,
    ) -> dict[str, Any]:
        """Compute aggregated statistics over agent history.

        Returns:
            Dict with keys: total, by_status, by_tool, avg_duration,
            total_cost, success_rate.
        """
        base_where = "WHERE 1=1"
        params: list[Any] = []

        if context_name:
            base_where += " AND context_name = ?"
            params.append(context_name)

        if since:
            base_where += " AND started_at >= ?"
            params.append(str(since))

        # Total count
        row = self.db.fetchone(
            f"SELECT COUNT(*) as cnt FROM agents {base_where}",
            tuple(params),
        )
        total = row["cnt"] if row else 0

        if total == 0:
            return {
                "total": 0,
                "by_status": {},
                "by_tool": {},
                "avg_duration_seconds": None,
                "total_cost": None,
                "success_rate": None,
            }

        # By status
        rows = self.db.fetchall(
            f"SELECT status, COUNT(*) as cnt FROM agents {base_where} GROUP BY status",
            tuple(params),
        )
        by_status = {row["status"]: row["cnt"] for row in rows}

        # By tool
        rows = self.db.fetchall(
            f"""SELECT json_extract(task_json, '$.tool') as tool, COUNT(*) as cnt
                FROM agents {base_where} GROUP BY tool""",
            tuple(params),
        )
        by_tool = {row["tool"]: row["cnt"] for row in rows}

        # Average duration (for terminal agents with both timestamps)
        row = self.db.fetchone(
            f"""SELECT AVG(
                    CAST((julianday(completed_at) - julianday(started_at)) * 86400 AS REAL)
                ) as avg_dur
                FROM agents {base_where}
                AND completed_at IS NOT NULL AND started_at IS NOT NULL""",
            tuple(params),
        )
        avg_duration = round(row["avg_dur"], 1) if row and row["avg_dur"] else None

        # Total cost
        row = self.db.fetchone(
            f"SELECT SUM(cost_estimate) as total_cost FROM agents {base_where}",
            tuple(params),
        )
        total_cost = round(row["total_cost"], 4) if row and row["total_cost"] else None

        # Success rate
        completed = by_status.get("completed", 0)
        terminal = sum(
            by_status.get(s, 0) for s in ("completed", "failed", "timeout", "killed")
        )
        success_rate = round(completed / terminal * 100, 1) if terminal > 0 else None

        return {
            "total": total,
            "by_status": by_status,
            "by_tool": by_tool,
            "avg_duration_seconds": avg_duration,
            "total_cost": total_cost,
            "success_rate": success_rate,
        }
