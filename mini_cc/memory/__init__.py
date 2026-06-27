"""3-tier declarative memory loader + writer.

Mirrors ``skills/loader.py`` for the read path, adds a project-tier
writer (``write_memory``) used by the ``memory_write`` tool. Agents
can only write to the project tier — system/tenant tiers are admin-owned.
"""
from __future__ import annotations

import re
from pathlib import Path

from ..plugins import (Memory as _PMemory, PluginTier, discover_memories,
                       tier_dir)


_NAME_RE = re.compile(r"[^a-zA-Z0-9._-]+")


def _sanitize(name: str) -> str:
    """Make a filename-safe memory name. Non-whitelisted chars → '-'."""
    cleaned = _NAME_RE.sub("-", name.strip()).strip(".-")
    return cleaned or "memory"


class MemoryLoader:
    """Reads 3-tier memories lazily; cached after first scan."""

    def __init__(self, project_root: Path,
                 tier_dirs: list[Path] | None = None,
                 data_dir: Path | None = None,
                 tenant_id: str | None = None):
        self.project_root = Path(project_root)
        self._tier_dirs = tier_dirs
        self._data_dir = Path(data_dir) if data_dir else None
        self._tenant_id = tenant_id
        self._registry: dict[str, _PMemory] = {}

    def _resolve_tier_dirs(self) -> list[Path]:
        if self._tier_dirs is not None:
            return self._tier_dirs
        if self._data_dir is not None and self._tenant_id is not None:
            return [
                tier_dir(self._data_dir, PluginTier.SYSTEM),
                tier_dir(self._data_dir, PluginTier.TENANT,
                         tenant_id=self._tenant_id),
                tier_dir(self.project_root, PluginTier.PROJECT),
            ]
        return []

    def scan(self) -> None:
        self._registry.clear()
        tier_dirs = self._resolve_tier_dirs()
        if tier_dirs:
            self._registry.update(discover_memories(tier_dirs))

    @property
    def registry(self) -> dict[str, _PMemory]:
        if not self._registry:
            self.scan()
        return self._registry

    def catalog(self) -> str:
        """One-line-per-memory listing."""
        if not self.registry:
            return "(no memories found)"
        return "\n".join(f"- {m.name} [{m.category}]: {m.description}"
                         for m in self._registry.values())

    def get(self, name: str) -> str:
        mem = self.registry.get(name)
        if mem is None:
            available = ", ".join(self._registry.keys()) or "(none)"
            return f"Memory not found: {name}. Available: {available}"
        return mem.content

    def recall(self, query: str, limit: int = 10) -> list[_PMemory]:
        """Keyword search across all memories. Simple substring match
        (case-insensitive) on name + description + body. Returns matches
        ranked by hit-count desc, capped at ``limit``.

        Future: swap in vector index (chromadb / sqlite-vec) for semantic
        search without changing the call site.
        """
        if not query.strip():
            return []
        q = query.lower()
        scored: list[tuple[int, _PMemory]] = []
        for m in self.registry.values():
            haystack = (m.name + "\n" + m.description + "\n" + m.content).lower()
            score = haystack.count(q)
            if score > 0:
                scored.append((score, m))
        scored.sort(key=lambda t: t[0], reverse=True)
        return [m for _, m in scored[:limit]]


def write_memory(project_root: Path, name: str, content: str,
                 category: str = "general",
                 description: str | None = None) -> Path:
    """Persist a new memory entry to the **project tier**.

    Returns the written file path. The name is sanitized to filename-safe
    chars; existing files with the same name are overwritten (agent can
    update its own notes).
    """
    safe = _sanitize(name)
    desc = description or content.split("\n", 1)[0].lstrip("#").strip()[:80]
    mem_dir = Path(project_root) / ".mini_cc" / "memory"
    mem_dir.mkdir(parents=True, exist_ok=True)
    fp = mem_dir / f"{safe}.md"
    body = content.strip()
    if body.startswith("---"):
        # Already has frontmatter; trust the caller.
        pass
    else:
        body = (f"---\nname: {safe}\n"
                f"description: {desc}\n"
                f"category: {category}\n---\n\n{body}")
    fp.write_text(body, encoding="utf-8")
    return fp
