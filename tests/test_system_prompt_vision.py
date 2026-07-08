"""System prompt: vision-gated section.

When MINI_CC_MODEL_VISION=false and the deployment has vision MCP tools
configured, the system prompt MUST include a section naming those tools
and instructing the model to call them with the image URL the hydrator
swapped in. Without this hint, the model just echoes the URL back.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from mini_cc.core.system_prompt import assemble_system_prompt


def _tool(name, description=""):
    return SimpleNamespace(name=name, description=description,
                           input_schema={})


def test_vision_tools_section_included_when_present(tmp_path: Path):
    """Defining behavior: vision_tools non-empty → prompt contains
    each tool's name + the 'use a vision tool' instruction."""
    prompt = assemble_system_prompt(
        project_root=tmp_path, tools=[],
        vision_tools=[
            _tool("mcp__zai-mcp-server__analyze_image",
                  "Analyze an image and return a description."),
            _tool("mcp__zai-mcp-server__extract_text_from_screenshot",
                  "OCR text from a screenshot."),
        ],
    )
    assert "mcp__zai-mcp-server__analyze_image" in prompt
    assert "mcp__zai-mcp-server__extract_text_from_screenshot" in prompt
    # The instruction wording.
    assert "vision tool" in prompt.lower()


def test_vision_tools_section_omitted_when_empty(tmp_path: Path):
    """No vision tools → no section, no hint. Backwards compat for
    deployments that don't gate vision."""
    prompt = assemble_system_prompt(
        project_root=tmp_path, tools=[], vision_tools=[])
    assert "vision tool" not in prompt.lower()
    # And the prompt still has the basic structure
    assert "Working directory" in prompt


def test_vision_tools_section_omitted_when_not_passed(tmp_path: Path):
    """Default behavior (kwarg omitted) — backwards compat."""
    prompt = assemble_system_prompt(project_root=tmp_path, tools=[])
    assert "vision tool" not in prompt.lower()


def test_vision_tools_section_explains_model_cannot_see_images(tmp_path: Path):
    """The model needs to know WHY there's a URL instead of an image —
    otherwise it may attempt to describe the URL itself. Pin the
    instructional phrasing."""
    prompt = assemble_system_prompt(
        project_root=tmp_path, tools=[],
        vision_tools=[_tool("mcp__v__analyze")])
    lower = prompt.lower()
    assert "cannot" in lower or "does not" in lower
    assert "image" in lower
