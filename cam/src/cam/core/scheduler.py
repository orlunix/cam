"""Task scheduler with DAG dependency resolution.

Supports declarative task definitions loaded from YAML files.
Tasks can depend on other tasks, forming a DAG that is validated
for cycles before execution. Independent tasks run in parallel.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Any

from cam.core.models import Agent, AgentStatus, TaskDefinition

logger = logging.getLogger(__name__)


class SchedulerError(Exception):
    """Error raised by the task scheduler."""


class TaskGraph:
    """Directed Acyclic Graph of task dependencies.

    Validates the graph structure and provides topological ordering
    for execution. Independent tasks at each level can run concurrently.
    """

    def __init__(self, tasks: list[TaskDefinition]) -> None:
        self._tasks: dict[str, TaskDefinition] = {}
        self._edges: dict[str, list[str]] = defaultdict(list)  # task -> depends_on

        for task in tasks:
            name = task.name
            if not name:
                raise SchedulerError("All tasks in a task file must have a 'name' field")
            if name in self._tasks:
                raise SchedulerError(f"Duplicate task name: '{name}'")
            self._tasks[name] = task
            for dep in task.depends_on:
                self._edges[name].append(dep)

        self._validate()

    def _validate(self) -> None:
        """Validate the DAG: check for missing deps and cycles."""
        # Check all dependencies reference existing tasks
        for task_name, deps in self._edges.items():
            for dep in deps:
                if dep not in self._tasks:
                    raise SchedulerError(
                        f"Task '{task_name}' depends on '{dep}' which is not defined"
                    )

        # Detect cycles using DFS
        visited: set[str] = set()
        in_stack: set[str] = set()

        def dfs(node: str) -> None:
            if node in in_stack:
                raise SchedulerError(f"Circular dependency detected involving task '{node}'")
            if node in visited:
                return
            in_stack.add(node)
            for dep in self._edges.get(node, []):
                dfs(dep)
            in_stack.discard(node)
            visited.add(node)

        for task_name in self._tasks:
            dfs(task_name)

    def execution_order(self) -> list[list[str]]:
        """Return tasks grouped by execution level.

        Tasks within the same level have no inter-dependencies and can run
        concurrently. Levels are ordered so all dependencies of level N
        are in levels < N.

        Returns:
            List of levels, each level is a list of task names.
        """
        # Compute in-degrees
        in_degree: dict[str, int] = {name: 0 for name in self._tasks}
        for task_name, deps in self._edges.items():
            in_degree[task_name] = len(deps)

        # Kahn's algorithm with level tracking
        levels: list[list[str]] = []
        ready = [name for name, deg in in_degree.items() if deg == 0]

        while ready:
            levels.append(sorted(ready))  # Sort for deterministic ordering
            next_ready: list[str] = []
            for completed_name in ready:
                # Find tasks that depend on this one
                for task_name, deps in self._edges.items():
                    if completed_name in deps:
                        in_degree[task_name] -= 1
                        if in_degree[task_name] == 0:
                            next_ready.append(task_name)
            ready = next_ready

        return levels

    def get_task(self, name: str) -> TaskDefinition:
        """Get a task by name."""
        return self._tasks[name]

    @property
    def task_names(self) -> list[str]:
        """All task names in the graph."""
        return list(self._tasks.keys())

    def __len__(self) -> int:
        return len(self._tasks)


class Scheduler:
    """Executes a TaskGraph using an AgentManager.

    Runs tasks level by level, parallelizing independent tasks within
    each level. Tracks results and handles failures.
    """

    def __init__(self, agent_manager: Any, context_store: Any) -> None:
        self._manager = agent_manager
        self._context_store = context_store

    async def execute(
        self,
        graph: TaskGraph,
        default_context: str | None = None,
        follow: bool = False,
    ) -> dict[str, Agent]:
        """Execute all tasks in the graph respecting dependencies.

        Args:
            graph: Validated TaskGraph to execute.
            default_context: Default context name for tasks without explicit context.
            follow: Whether to follow output in foreground.

        Returns:
            Dict mapping task name to final Agent state.

        Raises:
            SchedulerError: If a required dependency fails or context is missing.
        """
        results: dict[str, Agent] = {}
        levels = graph.execution_order()

        for level_idx, level in enumerate(levels):
            logger.info(
                "Executing level %d/%d: %s",
                level_idx + 1, len(levels), ", ".join(level),
            )

            # Launch all tasks in this level concurrently
            coros = []
            for task_name in level:
                task = graph.get_task(task_name)

                # Check dependencies succeeded
                for dep in task.depends_on:
                    dep_agent = results.get(dep)
                    if dep_agent is None or dep_agent.status != AgentStatus.COMPLETED:
                        dep_status = dep_agent.status.value if dep_agent else "not found"
                        raise SchedulerError(
                            f"Task '{task_name}' depends on '{dep}' which {dep_status}"
                        )

                # Resolve context
                ctx_name = task.context or default_context
                if not ctx_name:
                    raise SchedulerError(
                        f"Task '{task_name}' has no context and no default context specified"
                    )

                context = self._context_store.get(ctx_name)
                if not context:
                    raise SchedulerError(
                        f"Context '{ctx_name}' not found for task '{task_name}'"
                    )

                coros.append(self._run_task(task_name, task, context, follow))

            # Wait for all tasks in this level to complete
            level_results = await asyncio.gather(*coros, return_exceptions=True)

            for task_name, result in zip(level, level_results):
                if isinstance(result, Exception):
                    logger.error("Task '%s' raised exception: %s", task_name, result)
                    raise SchedulerError(
                        f"Task '{task_name}' failed with error: {result}"
                    ) from result
                results[task_name] = result

        return results

    async def _run_task(
        self,
        task_name: str,
        task: TaskDefinition,
        context: Any,
        follow: bool,
    ) -> Agent:
        """Run a single task via the agent manager."""
        logger.info("Starting task '%s' (tool=%s, context=%s)", task_name, task.tool, context.name)
        agent = await self._manager.run_agent(task, context, follow=follow)
        return agent


def load_task_file(path: str) -> tuple[list[TaskDefinition], dict[str, Any]]:
    """Load a task YAML file and return parsed TaskDefinitions.

    Args:
        path: Path to the YAML file.

    Returns:
        Tuple of (tasks, metadata) where metadata includes defaults and version.

    Raises:
        SchedulerError: If file cannot be loaded or parsed.
    """
    try:
        import yaml
    except ImportError:
        raise SchedulerError(
            "PyYAML is required for task files. Install with: pip install pyyaml"
        )

    from pathlib import Path as P

    file_path = P(path)
    if not file_path.exists():
        raise SchedulerError(f"Task file not found: {path}")

    try:
        with open(file_path) as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as e:
        raise SchedulerError(f"Invalid YAML in {path}: {e}")

    if not isinstance(data, dict):
        raise SchedulerError(f"Task file must be a YAML mapping, got {type(data).__name__}")

    # Extract defaults
    defaults = data.get("defaults", {})
    default_tool = defaults.get("tool", "claude")
    default_timeout_str = defaults.get("timeout")
    default_retry = defaults.get("retry", 0)

    # Parse timeout if provided
    default_timeout = None
    if default_timeout_str:
        from cam.core.config import parse_duration
        default_timeout = parse_duration(str(default_timeout_str))

    # Parse tasks
    raw_tasks = data.get("tasks", [])
    if not raw_tasks:
        raise SchedulerError(f"No tasks defined in {path}")

    from cam.core.models import RetryPolicy

    tasks: list[TaskDefinition] = []
    for raw in raw_tasks:
        if not isinstance(raw, dict):
            raise SchedulerError(f"Each task must be a mapping, got {type(raw).__name__}")

        name = raw.get("name")
        if not name:
            raise SchedulerError("Each task must have a 'name' field")

        tool = raw.get("tool", default_tool)
        prompt = raw.get("prompt")
        if not prompt:
            raise SchedulerError(f"Task '{name}' is missing required 'prompt' field")

        context = raw.get("context")
        depends_on = raw.get("depends_on", [])
        if isinstance(depends_on, str):
            depends_on = [depends_on]

        # Parse task-level timeout
        task_timeout_str = raw.get("timeout")
        if task_timeout_str:
            from cam.core.config import parse_duration
            timeout = parse_duration(str(task_timeout_str))
        else:
            timeout = default_timeout

        # Retry
        task_retry = raw.get("retry", default_retry)
        retry_policy = RetryPolicy(max_retries=int(task_retry))

        # Env vars
        env = raw.get("env", {})

        tasks.append(TaskDefinition(
            name=name,
            tool=tool,
            prompt=prompt,
            context=context,
            timeout=timeout,
            retry=retry_policy,
            env=env,
            depends_on=depends_on,
        ))

    metadata = {
        "version": data.get("version", "1"),
        "defaults": defaults,
        "task_count": len(tasks),
    }

    return tasks, metadata
