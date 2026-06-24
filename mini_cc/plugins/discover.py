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
    """Return every skill declared under ``<tier_dir>/skills/``."""
    skills_dir = tier_dir / "skills"
    out: dict[str, Skill] = {}
    if not skills_dir.exists():
        return out
    try:
        entries = sorted(skills_dir.iterdir())
    except OSError:
        return out
    for d in entries:
        if not d.is_dir():
            continue
        manifest = d / "SKILL.md"
        if not manifest.exists():
            continue
        try:
            raw = manifest.read_text(encoding="utf-8")
        except OSError as e:
            _log.warning("skill %s unreadable: %s", manifest, e)
            continue
        meta, body = _parse_frontmatter(raw)
        name = meta.get("name", d.name)
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


# ── MCP server discovery ──────────────────────────────────────────────

def write_mcp_servers(tier_dir: Path, servers: dict[str, dict]) -> None:
    """Persist ``servers`` to ``<tier_dir>/mcp.toml``.

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


def _read_mcp_toml(tier_dir: Path) -> dict[str, dict]:
    """Return the ``[name] -> spec`` dict from a tier's mcp.toml.

    Empty dict when the file is missing or unreadable. Spec is normalized
    to ``{"command": [...], "env": {...}, "cwd": "..."}`` shape.
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
        if not isinstance(spec, dict):
            continue
        cmd = spec.get("command")
        # Skip entries without a usable command — they can't boot anyway.
        if not (isinstance(cmd, list) and cmd):
            continue
        normalized: dict = {"command": [str(c) for c in cmd]}
        env = spec.get("env")
        if isinstance(env, dict):
            normalized["env"] = {str(k): str(v) for k, v in env.items()}
        cwd = spec.get("cwd")
        if isinstance(cwd, str):
            normalized["cwd"] = cwd
        out[name] = normalized
    return out


def discover_mcp_servers(tier_dirs: Iterable[Path]) -> dict[str, dict]:
    """Walk tiers in order; later tiers override earlier on name clash."""
    merged: dict[str, dict] = {}
    for td in tier_dirs:
        td = Path(td)
        if not td.exists():
            continue
        merged.update(_read_mcp_toml(td))
    return merged
