"""Web search tool backed by Tavily.

Tavily is a search API purpose-built for LLM agents — results arrive
as {title, url, content} dictionaries, no HTML scraping required.

The tool refuses cleanly when no API key is configured so the agent
can fall back to web_fetch (manual crawl) instead of failing the turn.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

from .base import FunctionTool, ToolContext
from ..config import default_config


TAVILY_ENDPOINT = "https://api.tavily.com/search"
DEFAULT_TIMEOUT = 20
MAX_RESULTS = 10


def _web_search(ctx: ToolContext, args: dict) -> str:
    query = (args.get("query") or "").strip()
    if not query:
        return "Error: query is required"

    cfg = default_config()
    api_key = cfg.tavily_api_key
    if not api_key:
        return ("Error: Tavily API key not configured. Set TAVILY_API_KEY "
                "(or MINI_CC_TAVILY_API_KEY) in the environment, or fall "
                "back to web_fetch for known URLs.")

    max_results = _clamp(int(args.get("max_results", 5)), 1, MAX_RESULTS)
    search_depth = args.get("search_depth", "basic")
    if search_depth not in ("basic", "advanced"):
        search_depth = "basic"
    timeout = _clamp(int(args.get("timeout", DEFAULT_TIMEOUT)), 1, 60)
    include_answer = bool(args.get("include_answer", False))

    payload = {
        "api_key": api_key,
        "query": query,
        "max_results": max_results,
        "search_depth": search_depth,
        "include_answer": include_answer,
    }
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        TAVILY_ENDPOINT, data=body,
        headers={"Content-Type": "application/json",
                 "Accept": "application/json"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(2_000_000)  # 2 MB hard ceiling
    except urllib.error.HTTPError as e:
        return f"Error: Tavily HTTP {e.code} {e.reason}"
    except urllib.error.URLError as e:
        return f"Error: Tavily network error: {e.reason}"
    except Exception as e:
        return f"Error: {type(e).__name__}: {e}"

    try:
        data = json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as e:
        return f"Error: Tavily returned non-JSON: {e}"

    return _format_results(query, data, search_depth, max_results)


def _format_results(query: str, data: dict, depth: str, max_r: int) -> str:
    """Render Tavily's response as compact text the LLM can quote from."""
    answer = data.get("answer")
    results = data.get("results") or []
    lines = [f"# Web search: {query!r}",
             f"_depth={depth} · max_results={max_r} · "
             f"returned={len(results)}_", ""]
    if answer:
        lines.append("**Quick answer:**")
        lines.append(answer.strip())
        lines.append("")
    if not results:
        lines.append("_no results_")
        return "\n".join(lines)
    lines.append("**Results:**")
    for i, r in enumerate(results, 1):
        title = (r.get("title") or "").strip() or "(untitled)"
        url = (r.get("url") or "").strip()
        content = (r.get("content") or "").strip()
        # Cap each result's content to keep the whole response bounded.
        if len(content) > 800:
            content = content[:800] + "…"
        score = r.get("score")
        score_tag = f" (score={score:.2f})" if isinstance(score, (int, float)) else ""
        lines.append(f"{i}. **{title}**{score_tag}")
        if url:
            lines.append(f"   {url}")
        if content:
            lines.append(f"   {content}")
        lines.append("")
    return "\n".join(lines).rstrip()


def _clamp(v: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, v))


WEB_SEARCH_TOOL = FunctionTool(
    name="web_search",
    description=(
        "Search the web via Tavily and return structured results "
        "({title, url, content}). Requires TAVILY_API_KEY to be set "
        "in the environment. Use this for current information, "
        "documentation, or answering factual questions; fall back to "
        "web_fetch when you already have a specific URL. Set "
        "include_answer=true for a Tavily-generated summary at the "
        "top of the response. search_depth='advanced' trades latency "
        "for more thorough results."),
    input_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query."},
            "max_results": {
                "type": "integer",
                "description": "Number of results (1-10). Default 5.",
            },
            "search_depth": {
                "type": "string", "enum": ["basic", "advanced"],
                "description": "'advanced' is slower but more thorough.",
            },
            "include_answer": {
                "type": "boolean",
                "description": "Include a Tavily-generated answer summary.",
            },
            "timeout": {
                "type": "integer",
                "description": "Request timeout (1-60s). Default 20.",
            },
        },
        "required": ["query"],
    },
    fn=_web_search,
    # M4-7: read-only — safe to run in the parallel batch.
    parallel_safe=True,
)


ALL = [WEB_SEARCH_TOOL]
