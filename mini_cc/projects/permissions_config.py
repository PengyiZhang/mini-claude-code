"""Per-project interactive-permissions config loader.

Reads `<workspace>/.mini_cc/permissions.toml`. Returns None if the
file is missing (interactive permissions disabled — today's behavior).
Malformed file logs a warning and also returns None.

Schema:

    prompt_tools = ["bash", "fs_write", "fs_edit"]
    timeout_seconds = 300   # optional; default 300

Unknown tool names in `prompt_tools` are silently ignored (no 400 —
the file is best-effort; users may list tools that don't exist yet).
"""
from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

_log = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 300


@dataclass
class PermissionsConfig:
    prompt_tools: set[str] = field(default_factory=set)
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS


def load_permissions_config(workspace: Path) -> PermissionsConfig | None:
    """Read <workspace>/.mini_cc/permissions.toml.

    Returns None when the file is missing or malformed. Returns an
    empty PermissionsConfig (no prompt_tools) when the file exists
    but is empty — that's still "interactive permissions enabled",
    just with no tools configured to prompt.
    """
    fp = Path(workspace) / ".mini_cc" / "permissions.toml"
    if not fp.exists():
        return None
    try:
        with fp.open("rb") as f:
            raw = tomllib.load(f)
    except (tomllib.TOMLDecodeError, OSError) as e:
        _log.warning("permissions config %s unreadable: %s", fp, e)
        return None

    tools_raw = raw.get("prompt_tools", [])
    if not isinstance(tools_raw, list):
        _log.warning("permissions config %s: prompt_tools must be a list", fp)
        return None
    # Coerce to set of non-empty strings; drop anything else silently.
    prompt_tools: set[str] = set()
    for t in tools_raw:
        if isinstance(t, str) and t:
            prompt_tools.add(t)

    timeout = raw.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
    if not isinstance(timeout, int) or timeout < 0:
        _log.warning("permissions config %s: bad timeout_seconds %r; using default",
                     fp, timeout)
        timeout = DEFAULT_TIMEOUT_SECONDS

    return PermissionsConfig(prompt_tools=prompt_tools,
                             timeout_seconds=timeout)
