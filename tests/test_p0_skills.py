"""SkillLoader + load_skill tool tests."""
from __future__ import annotations

from dataclasses import dataclass

from mini_cc.sandbox import SubprocessSandbox
from mini_cc.skills import SkillLoader
from mini_cc.storage import FSStorage
from mini_cc.tools import builtin_tools, dispatch
from mini_cc.tools.base import ToolContext


def _make_skill_file(root, name, description, body):
    d = root / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n{body}",
        encoding="utf-8")


def test_scan_picks_up_skills(tmp_path):
    _make_skill_file(tmp_path, "grep-tool",
                     "Search files for patterns", "Use grep wisely.")
    _make_skill_file(tmp_path, "commit-helper",
                     "Write good commit messages", "Be concise.")
    loader = SkillLoader(tmp_path)
    loader.scan()
    assert "grep-tool" in loader.registry
    assert "commit-helper" in loader.registry
    assert "Search files" in loader.registry["grep-tool"].description


def test_load_returns_full_content(tmp_path):
    _make_skill_file(tmp_path, "x", "desc", "Line one\nLine two")
    loader = SkillLoader(tmp_path)
    content = loader.load("x")
    assert "Line one" in content
    assert "Line two" in content


def test_load_unknown_skill_lists_available(tmp_path):
    _make_skill_file(tmp_path, "x", "desc", "body")
    loader = SkillLoader(tmp_path)
    out = loader.load("nonexistent")
    assert "not found" in out.lower()
    assert "x" in out  # available list


def test_catalog_format(tmp_path):
    _make_skill_file(tmp_path, "a", "alpha tool", "body")
    loader = SkillLoader(tmp_path)
    catalog = loader.catalog()
    assert "- a: alpha tool" in catalog


def test_empty_workspace_catalog(tmp_path):
    loader = SkillLoader(tmp_path)
    assert loader.catalog() == "(no skills found)"


def test_frontmatter_fallback_to_filename_when_missing(tmp_path):
    d = tmp_path / "skills" / "orphan"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("# Just a body\nNo frontmatter.", encoding="utf-8")
    loader = SkillLoader(tmp_path)
    loader.scan()
    # Falls back to directory name; description from first non-# line.
    assert "orphan" in loader.registry


def test_load_skill_tool_dispatches_to_loader(tmp_path):
    _make_skill_file(tmp_path, "demo", "a demo skill", "SKILL BODY HERE")
    loader = SkillLoader(tmp_path)
    sandbox = SubprocessSandbox("p", tmp_path / "ws")
    storage = FSStorage(tmp_path / "state")
    ctx = ToolContext(project_id="p", session_id="s", sandbox=sandbox,
                      storage=storage, todos=[], skills_loader=loader)

    tools = dispatch(builtin_tools())
    tool = tools["load_skill"]
    out = tool.handle(ctx, {"name": "demo"})
    assert "SKILL BODY HERE" in out


def test_load_skill_tool_without_loader(tmp_path):
    sandbox = SubprocessSandbox("p", tmp_path / "ws")
    storage = FSStorage(tmp_path / "state")
    ctx = ToolContext(project_id="p", session_id="s", sandbox=sandbox,
                      storage=storage, todos=[], skills_loader=None)
    tools = dispatch(builtin_tools())
    out = tools["load_skill"].handle(ctx, {"name": "x"})
    assert "not configured" in out.lower()


def test_project_wires_skills_loader(tmp_path):
    from mini_cc import ProjectManager
    pm = ProjectManager(tmp_path / "projects")
    p = pm.create(tenant_id="t1", project_id="a")
    _make_skill_file(p.workspace, "sk1", "skill one", "body")
    p.rescan_skills()
    ref = p.as_ref()
    assert "sk1" in ref.skills_catalog
    assert ref.skills_loader is not None
    assert "skill one" in ref.skills_loader.load("sk1")
