"""Structured JSONL logging for agent output."""

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from cam.constants import LOG_DIR


class AgentLogger:
    """Writes structured JSONL logs for a single agent.

    Each log entry is a JSON object with:
    - ts: ISO 8601 timestamp with timezone
    - agent_id: Unique agent identifier
    - type: Event type (e.g., "start", "output", "stop", "error")
    - data: Optional structured data
    - output: Optional text output

    Example:
        with AgentLogger("agent-123") as logger:
            logger.write("start", data={"command": "ls -la"})
            logger.write("output", output="file1.txt\\nfile2.txt")
            logger.write("stop", data={"exit_code": 0})
    """

    def __init__(self, agent_id: str, log_dir: Path | None = None):
        """Initialize logger.

        Args:
            agent_id: Unique identifier for the agent
            log_dir: Directory for log files (defaults to LOG_DIR from constants)
        """
        self.agent_id = agent_id
        self.log_dir = log_dir or LOG_DIR
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = self.log_dir / f"{agent_id}.jsonl"
        self._file = None

    def open(self):
        """Open log file for writing."""
        if self._file is None:
            self._file = open(self.log_path, "a", encoding="utf-8")

    def close(self):
        """Close log file."""
        if self._file:
            self._file.close()
            self._file = None

    def write(
        self, event_type: str, data: dict | None = None, output: str | None = None
    ):
        """Write a structured log entry.

        Args:
            event_type: Type of event (e.g., "start", "output", "stop", "error")
            data: Optional structured data as dict
            output: Optional text output
        """
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "agent_id": self.agent_id,
            "type": event_type,
        }
        if data:
            entry["data"] = data
        if output:
            entry["output"] = output

        if self._file:
            self._file.write(json.dumps(entry, ensure_ascii=False) + "\n")
            self._file.flush()

    def read_lines(self, tail: int | None = None) -> list[dict]:
        """Read log entries.

        Args:
            tail: If set, return only the last N entries

        Returns:
            List of log entries as dicts
        """
        if not self.log_path.exists():
            return []

        with open(self.log_path, "r", encoding="utf-8") as f:
            lines = f.readlines()

        if tail:
            lines = lines[-tail:]

        entries = []
        for line in lines:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    # Skip malformed lines
                    continue

        return entries

    def follow(self, poll_interval: float = 0.5):
        """Generator that yields new log entries as they appear (like tail -f).

        Args:
            poll_interval: Time in seconds between polls for new entries

        Yields:
            Dict entries as they are appended to the log
        """
        # Ensure file exists
        if not self.log_path.exists():
            self.log_path.touch()

        with open(self.log_path, "r", encoding="utf-8") as f:
            # Seek to end
            f.seek(0, 2)

            while True:
                line = f.readline()
                if line:
                    line = line.strip()
                    if line:
                        try:
                            yield json.loads(line)
                        except json.JSONDecodeError:
                            # Skip malformed lines
                            continue
                else:
                    # No new data, wait before polling again
                    time.sleep(poll_interval)

    def __enter__(self):
        """Context manager entry."""
        self.open()
        return self

    def __exit__(self, *args):
        """Context manager exit."""
        self.close()
