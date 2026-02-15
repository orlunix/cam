"""Transport factory for creating transport instances from configuration.

Supports lazy loading of transport implementations to avoid import overhead
and optional dependencies.
"""

from __future__ import annotations
import logging

from cam.core.models import MachineConfig, TransportType
from cam.transport.base import Transport
from cam.transport.local import LocalTransport

logger = logging.getLogger(__name__)


class TransportFactory:
    """Factory for creating Transport instances from MachineConfig."""

    @staticmethod
    def create(config: MachineConfig) -> Transport:
        """Create a Transport instance from machine configuration.

        Args:
            config: Machine configuration specifying transport type and parameters

        Returns:
            Transport instance configured according to the config

        Raises:
            ValueError: If transport type is unknown or invalid
            ImportError: If required transport implementation is not available
        """
        logger.debug(f"Creating transport for {config.type}")

        if config.type == TransportType.LOCAL:
            return LocalTransport(env_setup=config.env_setup)

        elif config.type == TransportType.SSH:
            # Lazy import to avoid circular dependencies and optional SSH deps
            try:
                from cam.transport.ssh import SSHTransport
            except ImportError as e:
                raise ImportError(
                    f"SSH transport requires additional dependencies: {e}\n"
                    "Install with: pip install cam[ssh]"
                ) from e

            return SSHTransport(
                host=config.host,
                user=config.user,
                port=config.port,
                key_file=config.key_file,
                env_setup=config.env_setup,
            )

        elif config.type == TransportType.DOCKER:
            # Lazy import for Docker support
            try:
                from cam.transport.docker import DockerTransport
            except ImportError as e:
                raise ImportError(
                    f"Docker transport requires additional dependencies: {e}\n"
                    "Install with: pip install cam[docker]"
                ) from e

            return DockerTransport(
                image=config.image,
                volumes=config.volumes,
            )

        elif config.type == TransportType.WEBSOCKET:
            # Lazy import for WebSocket client
            try:
                from cam.transport.websocket_client import WebSocketClient
            except ImportError as e:
                raise ImportError(
                    f"WebSocket transport requires additional dependencies: {e}\n"
                    "Install with: pip install cam[websocket]"
                ) from e

            return WebSocketClient(
                host=config.host,
                user=config.user,
                port=config.agent_port,
                auth_token=config.auth_token,
            )

        elif config.type == TransportType.OPENCLAW:
            # Lazy import for OpenClaw integration
            try:
                from cam.transport.openclaw import OpenClawTransport
            except ImportError as e:
                raise ImportError(
                    f"OpenClaw transport requires additional dependencies: {e}\n"
                    "Install with: pip install cam[openclaw]"
                ) from e

            # OpenClaw-specific configuration
            return OpenClawTransport(
                endpoint=config.openclaw_endpoint,
                api_key=config.openclaw_api_key,
                workspace=config.openclaw_workspace,
            )

        else:
            raise ValueError(
                f"Unknown transport type: {config.type}\n"
                f"Supported types: {', '.join(t.value for t in TransportType)}"
            )


# Convenience function for common use case
def create_transport(config: MachineConfig) -> Transport:
    """Create a transport instance from configuration.

    This is a convenience wrapper around TransportFactory.create().

    Args:
        config: Machine configuration

    Returns:
        Configured Transport instance
    """
    return TransportFactory.create(config)
