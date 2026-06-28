"""Tier-merging discovery for skills + MCP servers.

Both loaders walk a list of ``.mini_cc/`` tier dirs in priority order
(system first, project last) and produce a merged dict where later
tiers win on key conflicts.
"""
from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

_log = logging.getLogger(__name__)


@dataclass
class Skill:
    name: str
    description: str
    content: str


@dataclass
class Memory:
    """One declarative memory entry (project/tenant/system tier).

    Mirrors Skill but lives under ``.mini_cc/memory/<name>.md`` and adds
    an optional ``category`` field for grouping (preferences/decisions/
    issues/...). Same merge rule as skills: later tier wins on name clash.
    """
    name: str
    description: str
    category: str
    content: str


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    try:
        import yaml
        meta = yaml.safe_load(parts[1]) or {}
    except Exception:
        meta = {}
    return meta, parts[2].strip()


def _scan_skill_dir(tier_dir: Path) -> dict[str, Skill]:
    """Return every skill declared under ``<tier_dir>/skills/``.

    Supports two layouts:
      - flat:     ``skills/<name>/SKILL.md``
      - nested:   ``skills/<category>/<name>/SKILL.md``  (or deeper)

    Nested layouts are how third-party packs (e.g. superpowers) ship —
    a single tier can carry dozens of skills grouped by topic. The
    scanner walks every subdir recursively and picks up any SKILL.md
    it finds; the on-disk name (frontmatter ``name`` or the parent
    directory name) wins on conflict, with later tiers still overriding
    earlier ones via :func:`discover_skills`.
    """
    skills_dir = tier_dir / "skills"
    out: dict[str, Skill] = {}
    if not skills_dir.exists():
        return out
    try:
        # rglob instead of iterdir so nested category dirs are picked
        # up. Sorted for deterministic catalog ordering.
        manifests = sorted(skills_dir.rglob("SKILL.md"))
    except OSError:
        return out
    for manifest in manifests:
        try:
            raw = manifest.read_text(encoding="utf-8")
        except OSError as e:
            _log.warning("skill %s unreadable: %s", manifest, e)
            continue
        meta, body = _parse_frontmatter(raw)
        # Parent directory name is the canonical id when frontmatter
        # doesn't override. Walks like skills/architecture/<name>/SKILL.md
        # resolve to <name>, not "architecture".
        name = meta.get("name", manifest.parent.name)
        desc = (meta.get("description")
                or raw.split("\n", 1)[0].lstrip("#").strip())
        out[name] = Skill(name=name, description=desc, content=raw)
    return out


def discover_skills(tier_dirs: Iterable[Path]) -> dict[str, Skill]:
    """Walk tiers in order; later tiers override earlier on name clash."""
    merged: dict[str, Skill] = {}
    for td in tier_dirs:
        td = Path(td)
        if not td.exists():
            continue
        merged.update(_scan_skill_dir(td))
    return merged


# ── Memory discovery ────────────────────────────────────────────────────
#
# Memory entries live under ``<tier>/.mini_cc/memory/<name>.md``. They are
# like skills but meant for facts/decisions/preferences the agent should
# remember across sessions. Optional frontmatter fields:
#   name, description, category

def _scan_memory_dir(tier_dir: Path) -> dict[str, Memory]:
    """Return every memory declared under ``<tier_dir>/memory/``."""
    mem_dir = tier_dir / "memory"
    out: dict[str, Memory] = {}
    if not mem_dir.exists():
        return out
    try:
        entries = sorted(mem_dir.iterdir())
    except OSError:
        return out
    for f in entries:
        if not f.is_file() or f.suffix != ".md":
            continue
        try:
            raw = f.read_text(encoding="utf-8")
        except OSError as e:
            _log.warning("memory %s unreadable: %s", f, e)
            continue
        meta, body = _parse_frontmatter(raw)
        name = meta.get("name", f.stem)
        desc = meta.get("description") or raw.split("\n", 1)[0].lstrip("#").strip()
        category = meta.get("category", "general")
        out[name] = Memory(name=name, description=desc,
                           category=category, content=raw)
    return out


def discover_memories(tier_dirs: Iterable[Path]) -> dict[str, Memory]:
    """Walk tiers in order; later tiers override earlier on name clash."""
    merged: dict[str, Memory] = {}
    for td in tier_dirs:
        td = Path(td)
        if not td.exists():
            continue
        merged.update(_scan_memory_dir(td))
    return merged


# ── MCP server discovery ──────────────────────────────────────────────
#
# Two file formats are supported, in priority order within a tier:
#
#   1. ``.mcp.json``  — Claude Code format. Copy a Claude Code config
#      verbatim into ``.mini_cc/`` and it Just Works. Shape::
#
#          {"mcpServers": {
#              "name": {
#                  "type": "stdio"|"http"|"sse",   # default stdio
#                  "command": "npx",                 # stdio
#                  "args": ["-y", "pkg"],
#                  "env": {"KEY": "value"},
#                  "url": "https://...",             # http/sse
#                  "headers": {"Authorization": "..."}
#              }
#          }}
#
#   2. ``mcp.toml``   — mini_cc's original format. Shape::
#
#          [name]
#          command = ["npx", "mcp-server-docs"]
#          [name.env]
#          KEY = "value"
#
# When both files exist in the same tier, ``.mcp.json`` wins. Across
# tiers, the usual later-tier-wins rule applies.

VALID_TYPES = ("stdio", "http", "sse")


def write_mcp_servers(tier_dir: Path, servers: dict[str, dict]) -> None:
    """Persist ``servers`` to ``<tier_dir>/mcp.toml`` (legacy mini_cc shape).

    Used by admin tooling and tests; production reads via
    :func:`discover_mcp_servers`. The TOML shape is the natural one::

        [docs]
        command = ["npx", "mcp-server-docs"]

        [docs.env]
        KEY = "value"
    """
    tier_dir = Path(tier_dir)
    tier_dir.mkdir(parents=True, exist_ok=True)
    fp = tier_dir / "mcp.toml"
    lines: list[str] = []
    for name, spec in servers.items():
        cmd = spec.get("command") or []
        env = spec.get("env") or {}
        cwd = spec.get("cwd")
        cmd_repr = ", ".join(f'"{c}"' for c in cmd)
        lines.append(f"[{name}]")
        lines.append(f"command = [{cmd_repr}]")
        if cwd:
            lines.append(f'cwd = "{cwd}"')
        if env:
            lines.append(f"[{name}.env]")
            for k, v in env.items():
                lines.append(f'{k} = "{v}"')
        lines.append("")
    fp.write_text("\n".join(lines), encoding="utf-8")


def _normalize_spec(spec: dict) -> dict | None:
    """Coerce one server spec from either format into the unified shape::

        {"type": "stdio"|"http"|"sse",
         "command": [...],        # stdio only
         "env": {...}, "cwd": "...",
         "url": "...",            # http/sse only
         "headers": {...}}

    Returns None if the spec is unusable (no command, no url, unknown
    type, etc.) so the caller can silently skip it.
    """
    if not isinstance(spec, dict):
        return None
    declared_type = spec.get("type")
    cmd_val = spec.get("command")
    has_command_str = isinstance(cmd_val, str) and cmd_val
    has_command_list = isinstance(cmd_val, list) and cmd_val
    has_args_list = isinstance(spec.get("args"), list)
    has_url = isinstance(spec.get("url"), str) and spec["url"]

    # Type inference: Claude Code allows omitting ``type`` for stdio servers
    # when ``command`` is present. ``url`` without ``type`` ⇒ http.
    if declared_type is None:
        if has_url:
            inferred = "http"
        elif has_command_str or has_command_list or has_args_list:
            inferred = "stdio"
        else:
            return None
    else:
        inferred = str(declared_type).lower()
    if inferred not in VALID_TYPES:
        return None

    out: dict = {"type": inferred}
    if inferred == "stdio":
        # Claude Code: ``command`` is a string + ``args`` is a list.
        # mini_cc legacy TOML: ``command`` is already a list.
        if has_command_str:
            cmd = [str(cmd_val)]
            if has_args_list:
                cmd.extend(str(a) for a in spec["args"])
            out["command"] = cmd
        elif has_command_list:
            out["command"] = [str(c) for c in cmd_val]
        else:
            return None
        if isinstance(spec.get("env"), dict):
            out["env"] = {str(k): str(v) for k, v in spec["env"].items()}
        if isinstance(spec.get("cwd"), str) and spec["cwd"]:
            out["cwd"] = spec["cwd"]
    else:
        # http / sse — both need a URL.
        if not has_url:
            return None
        out["url"] = spec["url"]
        if isinstance(spec.get("headers"), dict):
            out["headers"] = {str(k): str(v)
                              for k, v in spec["headers"].items()}
        if isinstance(spec.get("env"), dict):
            # Some HTTP servers want env-derived secrets surfaced as headers
            # at call time; keep env around so callers can post-process.
            out["env"] = {str(k): str(v) for k, v in spec["env"].items()}
    return out


def _read_mcp_json(tier_dir: Path) -> dict[str, dict]:
    """Parse Claude Code's ``.mcp.json`` if present."""
    fp = Path(tier_dir) / ".mcp.json"
    if not fp.exists():
        return {}
    import json
    try:
        raw_text = fp.read_text(encoding="utf-8")
        raw = json.loads(raw_text)
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as e:
        _log.warning(".mcp.json %s unreadable: %s", fp, e)
        return {}
    if not isinstance(raw, dict):
        return {}
    servers = raw.get("mcpServers") or raw.get("mcp_servers") or {}
    if not isinstance(servers, dict):
        return {}
    out: dict[str, dict] = {}
    for name, spec in servers.items():
        normalized = _normalize_spec(spec)
        if normalized is not None:
            out[str(name)] = normalized
    return out


def _read_mcp_toml(tier_dir: Path) -> dict[str, dict]:
    """Return the ``[name] -> spec`` dict from a tier's mcp.toml.

    Empty dict when the file is missing or unreadable. Spec is normalized
    to the unified shape (``{"type": "stdio", "command": [...], ...}``).
    """
    fp = Path(tier_dir) / "mcp.toml"
    if not fp.exists():
        return {}
    try:
        with fp.open("rb") as f:
            raw = tomllib.load(f)
    except (tomllib.TOMLDecodeError, OSError) as e:
        _log.warning("mcp.toml %s unreadable: %s", fp, e)
        return {}

    out: dict[str, dict] = {}
    for name, spec in raw.items():
        # TOML entries don't carry a ``type`` field in the legacy shape —
        # they're always stdio. ``_normalize_spec`` infers it from command.
        normalized = _normalize_spec(spec)
        if normalized is not None:
            out[str(name)] = normalized
    return out


def _read_tier(tier_dir: Path) -> dict[str, dict]:
    """One tier's merged servers. ``.mcp.json`` overrides ``mcp.toml``
    on name clash within the same tier."""
    merged = _read_mcp_toml(tier_dir)
    merged.update(_read_mcp_json(tier_dir))
    return merged


def discover_mcp_servers(tier_dirs: Iterable[Path]) -> dict[str, dict]:
    """Walk tiers in order; later tiers override earlier on name clash."""
    merged: dict[str, dict] = {}
    for td in tier_dirs:
        td = Path(td)
        if not td.exists():
            continue
        merged.update(_read_tier(td))
    return merged
