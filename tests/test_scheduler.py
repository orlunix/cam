"""Tests for the task scheduler and DAG validation."""

from __future__ import annotations

import pytest

from cam.core.models import TaskDefinition
from cam.core.scheduler import SchedulerError, TaskGraph


def _task(name: str, depends_on: list[str] | None = None) -> TaskDefinition:
    return TaskDefinition(
        name=name,
        tool="claude",
        prompt=f"Do {name}",
        depends_on=depends_on or [],
    )


class TestTaskGraph:
    def test_single_task(self):
        graph = TaskGraph([_task("a")])
        assert len(graph) == 1
        assert graph.execution_order() == [["a"]]

    def test_independent_tasks(self):
        graph = TaskGraph([_task("a"), _task("b"), _task("c")])
        levels = graph.execution_order()
        assert len(levels) == 1
        assert sorted(levels[0]) == ["a", "b", "c"]

    def test_linear_dependencies(self):
        graph = TaskGraph([
            _task("a"),
            _task("b", depends_on=["a"]),
            _task("c", depends_on=["b"]),
        ])
        levels = graph.execution_order()
        assert levels == [["a"], ["b"], ["c"]]

    def test_diamond_dependencies(self):
        graph = TaskGraph([
            _task("a"),
            _task("b", depends_on=["a"]),
            _task("c", depends_on=["a"]),
            _task("d", depends_on=["b", "c"]),
        ])
        levels = graph.execution_order()
        assert levels[0] == ["a"]
        assert sorted(levels[1]) == ["b", "c"]
        assert levels[2] == ["d"]

    def test_cycle_detection(self):
        with pytest.raises(SchedulerError, match="Circular dependency"):
            TaskGraph([
                _task("a", depends_on=["b"]),
                _task("b", depends_on=["a"]),
            ])

    def test_missing_dependency(self):
        with pytest.raises(SchedulerError, match="not defined"):
            TaskGraph([
                _task("a", depends_on=["nonexistent"]),
            ])

    def test_duplicate_names(self):
        with pytest.raises(SchedulerError, match="Duplicate"):
            TaskGraph([_task("a"), _task("a")])

    def test_nameless_task(self):
        task = TaskDefinition(tool="claude", prompt="test")
        with pytest.raises(SchedulerError, match="name"):
            TaskGraph([task])

    def test_get_task(self):
        t = _task("my-task")
        graph = TaskGraph([t])
        assert graph.get_task("my-task").prompt == "Do my-task"
