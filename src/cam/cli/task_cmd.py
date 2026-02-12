"""Task file CLI commands.

Provides the `cam apply` command for declarative task execution from YAML files.
"""

from __future__ import annotations

from typing import Optional

import typer

from cam.cli.formatters import print_error, print_info, print_success, print_warning

app = typer.Typer(help="Task file operations", no_args_is_help=True)


def apply(
    file: str = typer.Option(..., "--file", "-f", help="Path to task YAML file"),
    ctx: Optional[str] = typer.Option(None, "--ctx", help="Default context name"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Validate and show plan without executing"),
    detach: bool = typer.Option(False, "--detach", help="Don't follow output"),
) -> None:
    """Apply tasks from a YAML file.

    Tasks can define dependencies forming a DAG. Independent tasks
    run in parallel, dependent tasks wait for their prerequisites.

    Examples:
        cam apply -f tasks.yaml
        cam apply -f tasks.yaml --ctx my-project
        cam apply -f tasks.yaml --dry-run
    """
    import asyncio

    from cam.cli.app import state
    from cam.core.scheduler import SchedulerError, TaskGraph, Scheduler, load_task_file

    # Load and parse task file
    try:
        tasks, metadata = load_task_file(file)
    except SchedulerError as e:
        print_error(str(e))
        raise typer.Exit(1)

    # Build DAG
    try:
        graph = TaskGraph(tasks)
    except SchedulerError as e:
        print_error(f"Invalid task graph: {e}")
        raise typer.Exit(1)

    levels = graph.execution_order()

    # Show plan
    print_info(f"Task file: {file} (v{metadata.get('version', '1')})")
    print_info(f"Tasks: {len(graph)} total, {len(levels)} execution levels")
    print_info("")

    for level_idx, level in enumerate(levels):
        level_label = f"Level {level_idx + 1}"
        task_details = []
        for task_name in level:
            task = graph.get_task(task_name)
            deps = f" (after: {', '.join(task.depends_on)})" if task.depends_on else ""
            ctx_label = task.context or ctx or "(default)"
            task_details.append(f"  {task_name}: [{task.tool}] {task.prompt[:60]}{deps} -> {ctx_label}")
        print_info(f"{level_label}:")
        for detail in task_details:
            print_info(detail)

    if dry_run:
        print_info("")
        print_success("Dry run complete — no tasks were executed")
        return

    # Execute
    print_info("")
    print_info("Executing task graph...")

    scheduler = Scheduler(state.agent_manager, state.context_store)

    try:
        results = asyncio.run(
            scheduler.execute(graph, default_context=ctx, follow=not detach)
        )

        # Summary
        print_info("")
        completed = sum(1 for a in results.values() if a.status.value == "completed")
        failed = sum(1 for a in results.values() if a.status.value in ("failed", "timeout", "killed"))
        running = sum(1 for a in results.values() if a.status.value == "running")

        for task_name, agent in results.items():
            status_icon = "✓" if agent.status.value == "completed" else "✗"
            print_info(f"  {status_icon} {task_name}: {agent.status.value} [{str(agent.id)[:8]}]")

        print_info("")
        if failed > 0:
            print_warning(f"Completed: {completed}, Failed: {failed}, Running: {running}")
        else:
            print_success(f"All {completed} tasks completed successfully")

    except SchedulerError as e:
        print_error(f"Scheduler error: {e}")
        raise typer.Exit(1)
    except Exception as e:
        print_error(f"Unexpected error: {e}")
        raise typer.Exit(1)
