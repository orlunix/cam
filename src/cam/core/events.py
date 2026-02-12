"""Simple synchronous publish/subscribe event bus for CAM.

Provides a lightweight internal event system for decoupled communication
between components. All events are AgentEvent instances from the core models.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Callable

from cam.core.models import AgentEvent


class EventBus:
    """Internal publish/subscribe event bus.

    Supports subscribing to specific event types by name, as well as
    a wildcard '*' subscription that receives all events.

    Handler errors are silently swallowed to prevent subscriber failures
    from breaking the publisher.

    Example:
        bus = EventBus()
        bus.subscribe("state_change", lambda e: print(e.detail))
        bus.subscribe("*", lambda e: log(e))
        bus.publish(event)
    """

    def __init__(self) -> None:
        """Initialize the event bus with empty handler registry."""
        self._handlers: dict[str, list[Callable]] = defaultdict(list)

    def subscribe(self, event_type: str, handler: Callable[[AgentEvent], None]) -> None:
        """Subscribe to events of a specific type.

        Args:
            event_type: Event type string to listen for, or '*' for all events.
            handler: Callable that accepts an AgentEvent. Must not raise
                     exceptions that should propagate to the publisher.
        """
        self._handlers[event_type].append(handler)

    def unsubscribe(self, event_type: str, handler: Callable) -> None:
        """Remove a handler from an event type.

        Args:
            event_type: Event type the handler was registered for.
            handler: The exact handler function/callable to remove.
                     Uses identity comparison (``is``), not equality.
        """
        self._handlers[event_type] = [
            h for h in self._handlers[event_type] if h is not handler
        ]

    def publish(self, event: AgentEvent) -> None:
        """Publish an event to all subscribers.

        First dispatches to handlers registered for the specific event_type,
        then dispatches to wildcard ('*') handlers.

        Handler exceptions are caught and silently ignored to ensure that
        a misbehaving subscriber cannot break the publisher or other subscribers.

        Args:
            event: The AgentEvent to publish.
        """
        # Dispatch to specific event type handlers
        for handler in self._handlers.get(event.event_type, []):
            try:
                handler(event)
            except Exception:
                pass  # Don't let handler errors break the publisher

        # Dispatch to wildcard handlers
        for handler in self._handlers.get("*", []):
            try:
                handler(event)
            except Exception:
                pass
