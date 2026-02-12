"""Shared test fixtures for CAM test suite."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from cam.core.models import Context, MachineConfig, TaskDefinition, TransportType
from cam.storage.database import Database


@pytest.fixture
def tmp_db(tmp_path):
    """Create a temporary SQLite database for testing."""
    db_path = tmp_path / "test.db"
    db = Database(db_path)
    yield db
    db.close()


@pytest.fixture
def context_store(tmp_db):
    """Create a ContextStore backed by a temp database."""
    from cam.storage.context_store import ContextStore
    return ContextStore(tmp_db)


@pytest.fixture
def agent_store(tmp_db):
    """Create an AgentStore backed by a temp database."""
    from cam.storage.agent_store import AgentStore
    return AgentStore(tmp_db)


@pytest.fixture
def sample_context():
    """Create a sample Context for testing."""
    from datetime import datetime, timezone
    from uuid import uuid4

    return Context(
        id=str(uuid4()),
        name="test-ctx",
        path="/tmp/test-project",
        machine=MachineConfig(type=TransportType.LOCAL),
        tags=["test"],
        created_at=datetime.now(timezone.utc),
    )


@pytest.fixture
def sample_task():
    """Create a sample TaskDefinition for testing."""
    return TaskDefinition(
        name="test-task",
        tool="claude",
        prompt="Write a hello world script",
    )


@pytest.fixture
def adapter_registry():
    """Create an AdapterRegistry with built-in adapters."""
    from cam.adapters.registry import AdapterRegistry
    return AdapterRegistry()
