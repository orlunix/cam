"""Adapter registry for managing tool adapters.

This module provides the AdapterRegistry class for discovering, registering,
and accessing tool adapters. Includes built-in adapters and supports custom
adapter loading.
"""

from __future__ import annotations

import importlib.util
import inspect
from pathlib import Path

from cam.adapters.base import ToolAdapter


class AdapterRegistry:
    """Registry for tool adapters.

    Provides built-in adapters (Claude Code, etc.) and supports registration
    of custom adapters. Manages adapter lookup and discovery.

    Attributes:
        _adapters: Dictionary mapping adapter names to adapter instances
    """

    def __init__(self) -> None:
        """Initialize registry and register built-in adapters."""
        self._adapters: dict[str, ToolAdapter] = {}
        self._register_builtins()

    def _register_builtins(self) -> None:
        """Register built-in tool adapters."""
        from cam.adapters.aider import AiderAdapter
        from cam.adapters.claude import ClaudeAdapter
        from cam.adapters.codex import CodexAdapter
        from cam.adapters.generic import GenericAdapter

        self.register(ClaudeAdapter())
        self.register(CodexAdapter())
        self.register(AiderAdapter())
        self.register(GenericAdapter())

    def register(self, adapter: ToolAdapter) -> None:
        """Register a tool adapter.

        Args:
            adapter: The adapter instance to register

        Raises:
            ValueError: If an adapter with the same name is already registered
        """
        if adapter.name in self._adapters:
            raise ValueError(
                f"Adapter '{adapter.name}' is already registered. "
                f"Use a unique name or unregister the existing adapter first."
            )
        self._adapters[adapter.name] = adapter

    def get(self, name: str) -> ToolAdapter | None:
        """Get an adapter by name.

        Args:
            name: The adapter name (e.g. "claude")

        Returns:
            The adapter instance, or None if not found
        """
        return self._adapters.get(name)

    def list(self) -> list[ToolAdapter]:
        """Get all registered adapters.

        Returns:
            List of all adapter instances
        """
        return list(self._adapters.values())

    def names(self) -> list[str]:
        """Get names of all registered adapters.

        Returns:
            List of adapter names
        """
        return list(self._adapters.keys())

    def load_custom(self, path: Path) -> None:
        """Load a custom adapter from a Python file.

        The file should contain a class that inherits from ToolAdapter.
        All ToolAdapter subclasses in the file will be discovered and registered.

        Args:
            path: Path to the Python file containing the adapter

        Raises:
            FileNotFoundError: If the file doesn't exist
            ValueError: If no ToolAdapter subclass is found in the file
            ImportError: If the module cannot be imported

        Example:
            >>> registry = AdapterRegistry()
            >>> registry.load_custom(Path("/path/to/my_adapter.py"))
        """
        if not path.exists():
            raise FileNotFoundError(f"Adapter file not found: {path}")

        if not path.is_file() or path.suffix != ".py":
            raise ValueError(f"Adapter path must be a Python file (.py): {path}")

        # Load the module from file
        spec = importlib.util.spec_from_file_location(path.stem, path)
        if spec is None or spec.loader is None:
            raise ImportError(f"Failed to load module spec from: {path}")

        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        # Find all ToolAdapter subclasses in the module
        adapters_found = []
        for name, obj in inspect.getmembers(module, inspect.isclass):
            # Check if it's a subclass of ToolAdapter (but not ToolAdapter itself)
            if (
                issubclass(obj, ToolAdapter)
                and obj is not ToolAdapter
                and obj.__module__ == module.__name__
            ):
                # Instantiate and register the adapter
                adapter_instance = obj()
                self.register(adapter_instance)
                adapters_found.append(adapter_instance.name)

        if not adapters_found:
            raise ValueError(
                f"No ToolAdapter subclass found in {path}. "
                f"Ensure your file contains a class inheriting from ToolAdapter."
            )

    def unregister(self, name: str) -> bool:
        """Unregister an adapter by name.

        Args:
            name: The adapter name to remove

        Returns:
            True if the adapter was removed, False if it wasn't registered
        """
        if name in self._adapters:
            del self._adapters[name]
            return True
        return False

    def __contains__(self, name: str) -> bool:
        """Check if an adapter is registered.

        Args:
            name: The adapter name to check

        Returns:
            True if the adapter is registered, False otherwise
        """
        return name in self._adapters

    def __len__(self) -> int:
        """Get the number of registered adapters.

        Returns:
            Count of registered adapters
        """
        return len(self._adapters)
