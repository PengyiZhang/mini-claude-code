"""AssetStore — per-project image asset persistence + Anthropic hydration.

Stores image bytes on disk under <root>/.assets/{asset_id}.{ext}, keeps an
assets.json index. asset_id = sha256(data)[:16] for natural dedup.
"""
from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from mini_cc.assets import AssetStore, AssetValidationError


@pytest.fixture
def store(tmp_path: Path) -> AssetStore:
    return AssetStore(tmp_path / ".assets")


def _png_bytes() -> bytes:
    # 1x1 PNG, minimal valid file
    return bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
        "890000000d49444154789c630001000000050001009c0d0a0e000000004945"
        "4e44ae426082"
    )


def test_put_writes_file_and_index(store: AssetStore, tmp_path: Path):
    data = _png_bytes()
    aid = store.put(data, media_type="image/png", src="test")

    assert len(aid) == 16
    # File written with .png extension
    files = list((tmp_path / ".assets").glob(f"{aid}.*"))
    assert files and files[0].read_bytes() == data
    # Index has meta
    idx = json.loads((tmp_path / ".assets" / "assets.json").read_text("utf-8"))
    assert aid in idx
    assert idx[aid]["media_type"] == "image/png"
    assert idx[aid]["bytes"] == len(data)


def test_put_dedups_by_sha256(store: AssetStore):
    data = _png_bytes()
    aid1 = store.put(data, media_type="image/png", src="a")
    aid2 = store.put(data, media_type="image/png", src="b")
    assert aid1 == aid2


def test_put_rejects_oversize(store: AssetStore):
    big = b"\0" * (5 * 1024 * 1024 + 1)
    with pytest.raises(AssetValidationError):
        store.put(big, media_type="image/png", src="test")


def test_put_rejects_unsupported_media_type(store: AssetStore):
    with pytest.raises(AssetValidationError):
        store.put(b"xxx", media_type="image/bmp", src="test")


def test_hydrate_block_returns_anthropic_shape(store: AssetStore):
    aid = store.put(_png_bytes(), media_type="image/png", src="test")
    block = store.hydrate_block(aid)
    assert block is not None
    assert block["type"] == "image"
    assert block["source"]["type"] == "base64"
    assert block["source"]["media_type"] == "image/png"
    # data round-trips back to original bytes
    assert base64.b64decode(block["source"]["data"]) == _png_bytes()


def test_hydrate_unknown_id_returns_none(store: AssetStore):
    assert store.hydrate_block("nonexistent123456") is None
