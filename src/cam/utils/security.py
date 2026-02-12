"""Security utilities for CAM.

Provides token encryption at rest and input sanitization.
Uses Fernet symmetric encryption with a machine-derived key
as a portable fallback (keyring used when available).
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import platform
from pathlib import Path

from cam.constants import DATA_DIR

logger = logging.getLogger(__name__)

# Token storage directory
TOKEN_DIR = DATA_DIR / "tokens"


def _get_machine_key() -> bytes:
    """Derive a machine-specific encryption key.

    Uses a combination of machine-specific values to derive a key
    that is consistent across sessions on the same machine but
    different across machines.

    Returns:
        32-byte key for Fernet encryption.
    """
    # Collect machine-specific entropy
    parts = [
        platform.node(),           # hostname
        str(os.getuid()),          # user ID
        platform.machine(),        # architecture
    ]

    # Add machine ID if available (Linux)
    machine_id_path = Path("/etc/machine-id")
    if machine_id_path.exists():
        try:
            parts.append(machine_id_path.read_text().strip())
        except OSError:
            pass

    raw = ":".join(parts).encode("utf-8")
    key_bytes = hashlib.sha256(raw).digest()
    return base64.urlsafe_b64encode(key_bytes)


def store_token(context_id: str, token: str) -> None:
    """Encrypt and store a token for a context.

    Tries OS keyring first, falls back to Fernet file encryption.

    Args:
        context_id: Context identifier (used as key).
        token: Plain-text token to store.
    """
    # Try keyring first
    try:
        import keyring
        keyring.set_password("cam", context_id, token)
        logger.debug("Token stored in system keyring for context %s", context_id)
        return
    except (ImportError, Exception):
        pass

    # Fallback: Fernet file encryption
    try:
        from cryptography.fernet import Fernet
    except ImportError:
        # Last resort: store in a permission-restricted file
        _store_plaintext(context_id, token)
        return

    key = _get_machine_key()
    f = Fernet(key)
    encrypted = f.encrypt(token.encode("utf-8"))

    TOKEN_DIR.mkdir(parents=True, exist_ok=True)
    token_path = TOKEN_DIR / f"{context_id}.enc"
    token_path.write_bytes(encrypted)
    token_path.chmod(0o600)  # Owner read/write only

    logger.debug("Token encrypted and stored at %s", token_path)


def retrieve_token(context_id: str) -> str | None:
    """Retrieve a stored token for a context.

    Tries OS keyring first, then checks for encrypted file.

    Args:
        context_id: Context identifier.

    Returns:
        Decrypted token string, or None if not found.
    """
    # Try keyring first
    try:
        import keyring
        token = keyring.get_password("cam", context_id)
        if token:
            return token
    except (ImportError, Exception):
        pass

    # Try Fernet file
    token_path = TOKEN_DIR / f"{context_id}.enc"
    if not token_path.exists():
        # Try plaintext fallback
        return _retrieve_plaintext(context_id)

    try:
        from cryptography.fernet import Fernet
    except ImportError:
        return _retrieve_plaintext(context_id)

    key = _get_machine_key()
    f = Fernet(key)

    try:
        encrypted = token_path.read_bytes()
        return f.decrypt(encrypted).decode("utf-8")
    except Exception as e:
        logger.warning("Failed to decrypt token for %s: %s", context_id, e)
        return None


def delete_token(context_id: str) -> bool:
    """Delete a stored token.

    Args:
        context_id: Context identifier.

    Returns:
        True if token was deleted, False if not found.
    """
    deleted = False

    # Try keyring
    try:
        import keyring
        keyring.delete_password("cam", context_id)
        deleted = True
    except (ImportError, Exception):
        pass

    # Try encrypted file
    token_path = TOKEN_DIR / f"{context_id}.enc"
    if token_path.exists():
        token_path.unlink()
        deleted = True

    # Try plaintext file
    plain_path = TOKEN_DIR / f"{context_id}.token"
    if plain_path.exists():
        plain_path.unlink()
        deleted = True

    return deleted


def _store_plaintext(context_id: str, token: str) -> None:
    """Store token in a permission-restricted plaintext file (last resort)."""
    TOKEN_DIR.mkdir(parents=True, exist_ok=True)
    token_path = TOKEN_DIR / f"{context_id}.token"
    token_path.write_text(token)
    token_path.chmod(0o600)
    logger.warning(
        "Token stored in plaintext at %s. Install 'cryptography' for encryption.",
        token_path,
    )


def _retrieve_plaintext(context_id: str) -> str | None:
    """Retrieve a plaintext token file."""
    token_path = TOKEN_DIR / f"{context_id}.token"
    if token_path.exists():
        return token_path.read_text().strip()
    return None


def sanitize_input(text: str, max_length: int = 10000) -> str:
    """Sanitize user input for safe use in commands.

    Removes null bytes and control characters, limits length.

    Args:
        text: Input text to sanitize.
        max_length: Maximum allowed length.

    Returns:
        Sanitized text string.
    """
    # Remove null bytes
    text = text.replace("\x00", "")

    # Limit length
    if len(text) > max_length:
        text = text[:max_length]

    return text
