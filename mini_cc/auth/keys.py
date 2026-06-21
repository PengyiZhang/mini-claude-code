"""Transport-agnostic tenant key registry.

JSON-backed mapping from API key to a KeyRecord (tenant_id, scopes,
expiry, label, timestamps). Used by the HTTP server's auth layer but
reusable by any future transport (WebSocket, gRPC) that needs
per-tenant authentication.

Key format: ``mck_<32 hex>``. Storage file is rewritten atomically on
every mutation (tmp + rename). A threading.Lock serializes writes
within a process; cross-process safety relies on atomic rename.

Backward compat: on read, bare-string values (the pre-Phase-D format)
are auto-promoted to KeyRecord with `scopes=["*"]`, `expires_at=None`,
`label="migrated"`. The file is not rewritten until the next
mutation, so existing fixtures stay byte-identical.
"""
from __future__ import annotations

import json
import os
import secrets
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .scope import parse_duration


_KEY_PREFIX = "mck_"
_KEY_HEX_BYTES = 16  # 32 hex chars


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds").replace("+00:00", "Z")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(s: str) -> datetime | None:
    if not s:
        return None
    try:
        # Tolerate either trailing Z or +00:00.
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


@dataclass
class KeyRecord:
    """Resolved identity + capabilities for a single API key."""
    key: str
    tenant_id: str
    scopes: list[str] = field(default_factory=lambda: ["*"])
    created_at: str = ""
    expires_at: str | None = None
    label: str = ""
    rotated_from: str | None = None

    def is_expired(self, now: datetime | None = None) -> bool:
        if not self.expires_at:
            return False
        dt = _parse_iso(self.expires_at)
        if dt is None:
            return False  # malformed → not expired (fail-open)
        return (now or _now()) >= dt

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class TenantPrincipal:
    """Back-compat alias. Earlier versions of the registry returned a
    bare tenant_id from ``lookup()``; callers that imported this name
    still get a usable identity container. Modern code should prefer
    ``KeyRecord``.
    """
    key: str
    tenant_id: str


class TenantKeyRegistry:
    """Thread-safe, JSON-backed ``api_key -> KeyRecord`` registry."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        if not self.path.exists():
            self._write({})

    # ── Public API ────────────────────────────────────────────────────
    def generate(self, tenant_id: str, *,
                 scopes: list[str] | None = None,
                 expires_in: str | int | None = None,
                 label: str = "") -> KeyRecord:
        """Create a new random key bound to ``tenant_id``."""
        if not tenant_id:
            raise ValueError("tenant_id must be non-empty")
        key = f"{_KEY_PREFIX}{secrets.token_hex(_KEY_HEX_BYTES)}"
        rec = KeyRecord(
            key=key,
            tenant_id=tenant_id,
            scopes=list(scopes) if scopes is not None else ["*"],
            created_at=_iso(_now()),
            expires_at=self._compute_expires_at(expires_in),
            label=label or "",
        )
        with self._lock:
            data = self._read_raw()
            data[key] = rec.to_dict()
            self._write(data)
        return rec

    def lookup(self, key: str) -> KeyRecord | None:
        """Return the KeyRecord for ``key`` or None if unknown / expired.

        Lazy expiry: expired records stay in the file but are rejected
        here. Use list_for() to see them.
        """
        if not key:
            return None
        with self._lock:
            raw = self._read_raw().get(key)
        if raw is None:
            return None
        rec = self._record_from_raw(key, raw)
        if rec.is_expired():
            return None
        return rec

    def list_for(self, tenant_id: str) -> list[KeyRecord]:
        """All keys (including expired) for the given tenant."""
        with self._lock:
            data = self._read_raw()
        out = []
        for k, raw in data.items():
            rec = self._record_from_raw(k, raw)
            if rec.tenant_id == tenant_id:
                out.append(rec)
        return out

    def revoke(self, key: str) -> bool:
        """Revoke ``key`` immediately. Returns True if it was present."""
        with self._lock:
            data = self._read_raw()
            if key not in data:
                return False
            del data[key]
            self._write(data)
            return True

    def rotate(self, key: str, *,
               grace_hours: int = 0,
               scopes: list[str] | None = None,
               expires_in: str | int | None = None,
               label: str = "") -> tuple[KeyRecord, KeyRecord | None]:
        """Issue a new key with the same tenant as ``key``. With
        ``grace_hours > 0``, the old key keeps working until
        ``now + grace_hours``; otherwise it's revoked immediately.

        Returns (new_record, old_record_or_None_if_revoked).
        """
        with self._lock:
            data = self._read_raw()
            raw = data.get(key)
            if raw is None:
                raise KeyError(f"unknown key: {key}")
            old = self._record_from_raw(key, raw)
            new_key = f"{_KEY_PREFIX}{secrets.token_hex(_KEY_HEX_BYTES)}"
            new_scopes = (list(scopes) if scopes is not None
                          else list(old.scopes))
            new_rec = KeyRecord(
                key=new_key,
                tenant_id=old.tenant_id,
                scopes=new_scopes,
                created_at=_iso(_now()),
                expires_at=self._compute_expires_at(expires_in),
                label=label or f"rotated from {key[:8]}…",
                rotated_from=key,
            )
            data[new_key] = new_rec.to_dict()
            if grace_hours > 0:
                old_rec = KeyRecord(
                    key=old.key,
                    tenant_id=old.tenant_id,
                    scopes=list(old.scopes),
                    created_at=old.created_at,
                    expires_at=_iso(_now() + timedelta(hours=grace_hours)),
                    label=old.label or "",
                    rotated_from=old.rotated_from,
                )
                data[key] = old_rec.to_dict()
                self._write(data)
                return new_rec, old_rec
            # Hard revoke
            del data[key]
            self._write(data)
            return new_rec, None

    def update(self, key: str, *,
               scopes: list[str] | None = None,
               expires_in: str | int | None = None,
               label: str | None = None) -> KeyRecord | None:
        """Patch fields on an existing key. Returns updated record, or
        None if key is unknown."""
        with self._lock:
            data = self._read_raw()
            raw = data.get(key)
            if raw is None:
                return None
            rec = self._record_from_raw(key, raw)
            if scopes is not None:
                rec.scopes = list(scopes)
            if expires_in is not None:
                rec.expires_at = self._compute_expires_at(expires_in)
            if label is not None:
                rec.label = label
            data[key] = rec.to_dict()
            self._write(data)
            return rec

    # ── Internal helpers ──────────────────────────────────────────────
    def _compute_expires_at(self, expires_in) -> str | None:
        if expires_in is None:
            return None
        secs = parse_duration(expires_in)
        if secs == 0:
            return None
        return _iso(_now() + timedelta(seconds=secs))

    def _record_from_raw(self, key: str, raw) -> KeyRecord:
        """Build a KeyRecord, tolerating the legacy bare-string shape."""
        if isinstance(raw, str):
            return KeyRecord(
                key=key, tenant_id=raw, scopes=["*"],
                created_at="", expires_at=None, label="migrated")
        if not isinstance(raw, dict):
            # Unknown shape — treat as no tenant so it never matches.
            return KeyRecord(key=key, tenant_id="", scopes=[],
                             created_at="", expires_at=None, label="malformed")
        # Be defensive about missing/extra fields.
        return KeyRecord(
            key=key,
            tenant_id=raw.get("tenant_id", ""),
            scopes=list(raw.get("scopes", ["*"])),
            created_at=raw.get("created_at", ""),
            expires_at=raw.get("expires_at"),
            label=raw.get("label", ""),
            rotated_from=raw.get("rotated_from"),
        )

    def _read_raw(self) -> dict:
        """Load the on-disk file. Tolerates corrupt / missing file."""
        if not self.path.exists():
            return {}
        try:
            with self.path.open("r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}

    def _write(self, data: dict) -> None:
        """Serialize to disk. Bare-string legacy values are promoted to
        KeyRecord shape on write, so the first mutation after a v0
        keys.json normalizes the whole file."""
        normalized = {}
        for k, raw in data.items():
            if isinstance(raw, str):
                rec = KeyRecord(
                    key=k, tenant_id=raw, scopes=["*"],
                    created_at="", expires_at=None, label="migrated")
                normalized[k] = rec.to_dict()
            elif isinstance(raw, dict):
                normalized[k] = raw
            # Unknown shapes get filtered out (they'd never resolve to
            # a usable KeyRecord anyway).
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(normalized, f, indent=2)
        os.replace(tmp, self.path)
