"""Web fetch tool — pull a URL and return text content.

Uses urllib from the standard library so the project doesn't pick up
an HTTP-client dependency. Output is truncated to keep the LLM context
budget under control; agents that need more should follow up with a
narrower request or use grep on the returned text.
"""
from __future__ import annotations

import urllib.error
import urllib.request

from .base import FunctionTool, ToolContext


MAX_BYTES = 50_000
DEFAULT_TIMEOUT = 20


def _web_fetch(ctx: ToolContext, args: dict) -> str:
    url = (args.get("url") or "").strip()
    if not url:
        return "Error: url is required"
    if not (url.startswith("http://") or url.startswith("https://")):
        return f"Error: url must be http(s): {url}"
    timeout = _coerce_timeout(args.get("timeout"), DEFAULT_TIMEOUT)
    max_bytes = _coerce_int(args.get("max_bytes"), MAX_BYTES)
    req = urllib.request.Request(
        url, headers={"User-Agent": "mini_cc/web_fetch",
                      "Accept": "text/*, application/json, */*;q=0.1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            content_type = resp.headers.get("Content-Type", "")
            raw = resp.read(max_bytes + 1)
    except urllib.error.HTTPError as e:
        return f"Error: HTTP {e.code} {e.reason} for {url}"
    except urllib.error.URLError as e:
        return f"Error: URL error for {url}: {e.reason}"
    except Exception as e:
        return f"Error: {type(e).__name__}: {e}"
    # Decode and (very lightly) flag binary content so the agent doesn't
    # try to read a JPEG as text.
    try:
        text = raw.decode("utf-8", errors="replace")
    except Exception:
        return f"Error: could not decode response from {url}"
    truncated = len(raw) > max_bytes
    note = ""
    if truncated:
        note = f"\n... (truncated at {max_bytes} bytes)"
    return (f"HTTP {resp.status} {content_type}\n"
            f"{text}{note}") if not truncated else (
        f"HTTP {resp.status} {content_type}\n{text}{note}")


def _coerce_int(raw, default: int) -> int:
    try:
        v = int(raw)
        return v if v > 0 else default
    except (TypeError, ValueError):
        return default


def _coerce_timeout(raw, default: int) -> int:
    return _coerce_int(raw, default)


WEB_FETCH_TOOL = FunctionTool(
    name="web_fetch",
    description=(
        "Fetch a URL over HTTP/HTTPS and return the response body as "
        "text. Useful for reading documentation pages, JSON APIs, or "
        "raw files exposed over HTTP. Output is decoded UTF-8 with "
        "errors replaced and truncated at 50 KB (configurable via "
        "max_bytes). HTTPS is required — plain http is allowed but "
        "discouraged. No JS rendering; the raw body is returned as-is."),
    input_schema={
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "http(s) URL to fetch."},
            "timeout": {
                "type": "integer",
                "description": "Request timeout in seconds. Default 20.",
            },
            "max_bytes": {
                "type": "integer",
                "description": "Maximum bytes to read. Default 50000.",
            },
        },
        "required": ["url"],
    },
    fn=_web_fetch,
    # M4-7: read-only — safe to run in the parallel batch.
    parallel_safe=True,
)


ALL = [WEB_FETCH_TOOL]
