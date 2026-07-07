"""AssetStore — per-project image asset persistence + Anthropic hydration."""
from __future__ import annotations

import hashlib
import json
import logging
import time
from pathlib import Path

from .server.errors import BadRequest

log = logging.getLogger(__name__)

MAX_BYTES = 5 * 1024 * 1024
ALLOWED_MEDIA_TYPES = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
}


class AssetValidationError(BadRequest):
    """Raised when upload violates size/format constraints. Maps to HTTP 400."""


class AssetStore:
    """Stores image bytes on disk + JSON index. Asset IDs are sha256[:16]
    of the content, so identical bytes dedup naturally. hydrate_block()
    produces the Anthropic image block shape on demand at LLM call time."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._index_path = self.root / "assets.json"
        self._index: dict[str, dict] = self._load_index()

    def _load_index(self) -> dict[str, dict]:
        if not self._index_path.exists():
            return {}
        try:
            return json.loads(self._index_path.read_text("utf-8"))
        except Exception:
            log.exception("assets.json corrupt, starting empty")
            return {}

    def _save_index(self) -> None:
        tmp = self._index_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._index, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(self._index_path)

    def put(self, data: bytes, *, media_type: str, src: str) -> str:
        if media_type not in ALLOWED_MEDIA_TYPES:
            raise AssetValidationError(
                f"unsupported media_type: {media_type}; allowed: "
                f"{sorted(ALLOWED_MEDIA_TYPES)}")
        if len(data) > MAX_BYTES:
            raise AssetValidationError(
                f"asset too large: {len(data)} bytes (max {MAX_BYTES})")

        asset_id = hashlib.sha256(data).hexdigest()[:16]
        if asset_id in self._index:
            return asset_id  # dedup

        ext = ALLOWED_MEDIA_TYPES[media_type]
        path = self.root / f"{asset_id}.{ext}"
        path.write_bytes(data)
        self._index[asset_id] = {
            "media_type": media_type,
            "bytes": len(data),
            "sha256": asset_id,  # first 16 of full hash; full hash not stored
            "src": src,
            "created_at": time.time(),
        }
        self._save_index()
        return asset_id

    def get_meta(self, asset_id: str) -> dict | None:
        return self._index.get(asset_id)

    def get_path(self, asset_id: str) -> Path | None:
        meta = self._index.get(asset_id)
        if meta is None:
            return None
        ext = ALLOWED_MEDIA_TYPES.get(meta["media_type"], "bin")
        p = self.root / f"{asset_id}.{ext}"
        return p if p.exists() else None

    def hydrate_block(self, asset_id: str) -> dict | None:
        import base64
        path = self.get_path(asset_id)
        if path is None:
            return None
        meta = self._index[asset_id]
        data = path.read_bytes()
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": meta["media_type"],
                "data": base64.b64encode(data).decode("ascii"),
            },
        }
