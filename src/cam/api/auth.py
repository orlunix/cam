"""Token-based authentication for CAM API Server."""

from __future__ import annotations

import secrets

from fastapi import Header, HTTPException, status


def generate_token() -> str:
    """Generate a cryptographically secure API token."""
    return secrets.token_urlsafe(32)


class TokenAuth:
    """Callable dependency for validating Bearer tokens on REST endpoints.

    Usage in route handlers:
        await state.token_auth(authorization=request.headers.get("authorization"))
    """

    def __init__(self, expected_token: str) -> None:
        self._expected_token = expected_token

    async def __call__(
        self,
        authorization: str | None = None,
    ) -> str:
        if not authorization:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing Authorization header",
            )
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or token != self._expected_token:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired token",
            )
        return token


class WSTokenAuth:
    """Validate token from WebSocket query parameter."""

    def __init__(self, expected_token: str) -> None:
        self._expected_token = expected_token

    def validate(self, token: str | None) -> bool:
        return token == self._expected_token
