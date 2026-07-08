"""MCPPool.vision_tools() — auto-discover MCP tools that accept an image.

When MINI_CC_MODEL_VISION=false, the agent loop mints a signed URL for
each attached image and asks the model to pass it to a vision-capable
MCP tool. Discovery is by parameter-name heuristic (``image``, ``url``,
``screenshot``, ...) plus a description keyword fallback — good enough
for the common cases (zai-mcp-server, playwright-mcp) without
requiring a tool registry.
"""
from __future__ import annotations

from mini_cc.mcp.client import MCPPool


class _FakeClient:
    """Minimal stand-in: just enough of MCPClient for all_tools /
    vision_tools to iterate."""
    def __init__(self, tools):
        self.tools = tools

    def call_tool(self, name, args):
        return f"called {name}"


def _tool(name, schema_props=None, description=""):
    schema = {"type": "object", "properties": schema_props or {}}
    return {"name": name, "description": description, "inputSchema": schema}


def _pool():
    return MCPPool(project_id="p1")


def test_vision_tools_returns_tools_with_image_param():
    """The defining case: a tool whose inputSchema includes an
    image-like parameter name (image, imageSource, image_url, ...) is
    classified as a vision tool."""
    pool = _pool()
    pool._clients["zai-mcp-server"] = _FakeClient([
        _tool("analyze_image", {"imageSource": {"type": "string"}}),
        _tool("extract_text_from_screenshot", {"image": {"type": "string"}}),
    ])
    vision = pool.vision_tools()
    names = sorted(t.name for t in vision)
    assert names == [
        "mcp__zai-mcp-server__analyze_image",
        "mcp__zai-mcp-server__extract_text_from_screenshot",
    ]


def test_vision_tools_falls_back_to_description_keyword():
    """Some tools don't follow the param-name convention but telegraph
    intent in the description."""
    pool = _pool()
    pool._clients["helpers"] = _FakeClient([
        _tool("see", description="Look at a screenshot and describe it"),
        _tool("bash", description="Run a shell command"),
    ])
    vision = pool.vision_tools()
    names = [t.name for t in vision]
    assert "mcp__helpers__see" in names
    assert "mcp__helpers__bash" not in names


def test_vision_tools_empty_when_pool_lacks_them():
    """Pure-bash pool returns []. The agent loop checks this before
    emitting the system-prompt hint."""
    pool = _pool()
    pool._clients["shell"] = _FakeClient([
        _tool("bash", {"command": {"type": "string"}}),
        _tool("read_file", {"path": {"type": "string"}}),
    ])
    assert pool.vision_tools() == []


def test_vision_tools_empty_when_pool_empty():
    """No clients connected → no vision tools. Must not crash."""
    pool = _pool()
    assert pool.vision_tools() == []


def test_vision_tools_dedupes_across_param_and_description_match():
    """A tool matching BOTH heuristics (image param + image in desc)
    should appear once, not twice."""
    pool = _pool()
    pool._clients["z"] = _FakeClient([
        _tool("analyze", {"image": {"type": "string"}},
              description="analyze an image"),
    ])
    assert len(pool.vision_tools()) == 1


def test_vision_tools_includes_common_image_param_variants():
    """Pin the parameter-name set so we don't regress and miss a common
    variant. Anything matching ``image``, ``image_url``, ``image_path``,
    ``imageSource``, ``picture``, ``screenshot`` qualifies."""
    pool = _pool()
    pool._clients["v"] = _FakeClient([
        _tool("a", {"image": {}}),
        _tool("b", {"image_url": {}}),
        _tool("c", {"image_path": {}}),
        _tool("d", {"imageSource": {}}),
        _tool("e", {"picture": {}}),
        _tool("f", {"screenshot": {}}),
        _tool("g", {"url": {}}),  # too generic — should NOT match by name alone
    ])
    names = sorted(t.name.split("__")[-1] for t in pool.vision_tools())
    assert "g" not in names  # bare 'url' is too generic
    assert names == ["a", "b", "c", "d", "e", "f"]
