"""CAM API Server — FastAPI application.

Wraps the Core layer (AgentManager, EventBus, Storage) and exposes it
over REST endpoints + WebSocket event streaming.
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from cam.api.auth import TokenAuth, WSTokenAuth, generate_token

logger = logging.getLogger(__name__)


class ServerState:
    """Shared state for the API server, analogous to CLI's AppState.

    Created eagerly during lifespan startup. Stored on ``app.state.server``
    and accessed by route handlers via ``request.app.state.server``.
    """

    def __init__(self, overrides: dict | None = None) -> None:
        from cam.adapters.registry import AdapterRegistry
        from cam.core.agent_manager import AgentManager
        from cam.core.config import load_config
        from cam.core.events import EventBus
        from cam.storage.agent_store import AgentStore
        from cam.storage.context_store import ContextStore
        from cam.storage.database import Database

        self.config = load_config(**(overrides or {}))
        data_dir = Path(self.config.paths.data_dir).expanduser()
        data_dir.mkdir(parents=True, exist_ok=True)

        self.db = Database(data_dir / "cam.db")
        self.context_store = ContextStore(self.db)
        self.agent_store = AgentStore(self.db)
        self.event_bus = EventBus()
        self.adapter_registry = AdapterRegistry()
        self.agent_manager = AgentManager(
            config=self.config,
            context_store=self.context_store,
            agent_store=self.agent_store,
            event_bus=self.event_bus,
            adapter_registry=self.adapter_registry,
        )
        self.started_at = time.time()

        # Auth
        token = self.config.server.auth_token or generate_token()
        self.auth_token = token
        self.token_auth = TokenAuth(token)
        self.ws_token_auth = WSTokenAuth(token)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Create shared state on startup, clean up on shutdown."""
    overrides = getattr(app.state, "_overrides", None)
    server_state = ServerState(overrides=overrides)
    app.state.server = server_state

    logger.info(
        "CAM API Server started on %s:%d",
        server_state.config.server.host,
        server_state.config.server.port,
    )
    logger.info("Auth token: %s", server_state.auth_token)

    # Start relay connector if configured
    relay_task = None
    relay_url = server_state.config.server.relay_url
    if relay_url:
        from cam.api.relay_connector import relay_loop

        relay_token = server_state.config.server.relay_token
        relay_task = asyncio.create_task(
            relay_loop(relay_url, relay_token, app)
        )
        logger.info("Relay connector started → %s", relay_url)

    yield

    if relay_task and not relay_task.done():
        relay_task.cancel()
        try:
            await relay_task
        except asyncio.CancelledError:
            pass
    logger.info("CAM API Server stopped")


def create_app(overrides: dict | None = None) -> FastAPI:
    """Factory function that creates the FastAPI app.

    Args:
        overrides: Config overrides passed to ``load_config()``.

    Returns:
        Configured FastAPI application.
    """
    app = FastAPI(
        title="CAM API Server",
        description="Coding Agent Manager — REST + WebSocket API",
        version="0.1.0",
        lifespan=lifespan,
    )

    # Store overrides for lifespan to pick up
    app.state._overrides = overrides

    # CORS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Register routes
    from cam.api.routes.agents import router as agents_router
    from cam.api.routes.contexts import router as contexts_router
    from cam.api.routes.system import router as system_router
    from cam.api.ws import router as ws_router

    app.include_router(agents_router, prefix="/api")
    app.include_router(contexts_router, prefix="/api")
    app.include_router(system_router, prefix="/api")
    app.include_router(ws_router, prefix="/api")

    # Serve web client (PWA) — mount AFTER API routes
    web_dir = Path(__file__).parent.parent.parent.parent / "web"
    if web_dir.exists():
        from fastapi.staticfiles import StaticFiles

        app.mount("/", StaticFiles(directory=str(web_dir), html=True), name="web")

    return app


def main() -> None:
    """Entry point for ``cam-server`` script."""
    import uvicorn

    from cam.core.config import load_config

    config = load_config()
    app = create_app()
    uvicorn.run(
        app,
        host=config.server.host,
        port=config.server.port,
        log_level=config.server.log_level,
    )
