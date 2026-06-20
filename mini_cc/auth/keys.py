"""Transport-agnostic tenant key registry.

JSON-backed mapping from API key to tenant_id. Used by the HTTP
server's auth layer but reusable by any future transport (WebSocket,
gRPC) that needs per-tenant authentication.

Key format: ``mck_<32 hex>``. Storage file is rewritten atomically
on every mutation (tmp + rename). A threading.Lock serializes writes
within a process; cross-process safety relies on atomic rename.
"""
from __future__ import annotations

import json
import os
import secrets
import threading
from dataclasses import dataclass
from pathlib import Path


_KEY_PREFIX = "mck_"
_KEY_HEX_BYTES = 16  # 32 hex chars


@dataclass
class TenantPrincipal:
    """A resolved tenant identity. Returned by lookup-style helpers
    when the caller wants both the key and tenant_id."""
    key: str
    tenant_id: str


class TenantKeyRegistry:
    """Thread-safe, JSON-backed ``api_key -> tenant_id`` registry."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        if not self.path.exists():
            self._write({})

    # ── Public API ────────────────────────────────────────────────────
    def generate(self, tenant_id: str) -> str:
        """Create a new random key bound to ``tenant_id``. Returns the key."""
        if not tenant_id:
            raise ValueError("tenant_id must be non-empty")
        key = f"{_KEY_PREFIX}{secrets.token_hex(_KEY_HEX_BYTES)}"
        with self._lock:
            data = self._read()
            data[key] = tenant_id
            self._write(data)
        return key

    def lookup(self, key: str) -> str | None:
        """Return the tenant_id for ``key`` or None if unknown / revoked."""
        if not key:
            return None
        return self._read().get(key)

    def list_for(self, tenant_id: str) -> list[str]:
        """All active keys for the given tenant."""
        data = self._read()
        return [k for k, tid in data.items() if tid == tenant_id]

    def revoke(self, key: str) -> bool:
        """Revoke ``key``. Returns True if it was present, False otherwise."""
        with self._lock:
            data = self._read()
            if key not in data:
                return False
            del data[key]
            self._write(data)
            return True

    # ── Internal helpers ──────────────────────────────────────────────
    def _read(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        try:
            with self.path.open("r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}

    def _write(self, data: dict[str, str]) -> None:
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, self.path)
