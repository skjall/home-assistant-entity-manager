"""Generated API token for external, read-only access to the rename log.

A single token is generated on demand (never user-chosen), shown to the user
exactly once, and persisted only as a SHA-256 hash in ``/data`` so the plaintext
never lives on disk. Verification hashes the presented token and compares it to
the stored hash in constant time.
"""

from datetime import datetime, timezone
import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
from typing import Any, Dict

logger = logging.getLogger(__name__)

# Prefix makes a leaked token recognisable as belonging to this add-on.
TOKEN_PREFIX = "em_"


def _hash(token: str) -> str:
    """Return the hex SHA-256 of a token."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class ApiTokenStore:
    """Persists a single hashed API token in a JSON file."""

    def __init__(self, path: str) -> None:
        """Initialise the store backed by ``path``; create its directory."""
        self.path = path
        self._lock = threading.Lock()
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)

    def _read(self) -> Dict[str, Any]:
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError) as error:
            logger.warning("Failed to read API token store: %s", error)
            return {}

    def _write(self, data: Dict[str, Any]) -> None:
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump(data, handle)

    def exists(self) -> bool:
        """True when a token has been generated and not revoked."""
        return bool(self._read().get("hash"))

    def generate(self) -> str:
        """Generate a new token, persist only its hash, return the plaintext once.

        Replaces (and thereby invalidates) any previous token.
        """
        token = TOKEN_PREFIX + secrets.token_urlsafe(32)
        with self._lock:
            self._write(
                {
                    "hash": _hash(token),
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "last_used_at": None,
                }
            )
        return token

    def verify(self, token: str) -> bool:
        """Return True when ``token`` matches the stored hash (constant time)."""
        if not token:
            return False
        with self._lock:
            data = self._read()
            stored = data.get("hash")
            if not stored or not hmac.compare_digest(_hash(token), stored):
                return False
            data["last_used_at"] = datetime.now(timezone.utc).isoformat()
            try:
                self._write(data)
            except OSError as error:
                logger.warning("Failed to update API token last_used_at: %s", error)
            return True

    def revoke(self) -> None:
        """Delete the stored token so no token is accepted anymore."""
        with self._lock:
            try:
                if os.path.exists(self.path):
                    os.remove(self.path)
            except OSError as error:
                logger.warning("Failed to revoke API token: %s", error)

    def status(self) -> Dict[str, Any]:
        """Return token metadata for the UI; never exposes the token itself."""
        data = self._read()
        return {
            "configured": bool(data.get("hash")),
            "created_at": data.get("created_at"),
            "last_used_at": data.get("last_used_at"),
        }
