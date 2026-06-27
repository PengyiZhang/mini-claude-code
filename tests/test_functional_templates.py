"""F6.1 — Project templates: bundled templates + apply_template."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "mini_cc"))

from mini_cc.projects.templates import (  # noqa: E402
    apply_template, get_template, list_templates,
)


def test_bundled_templates_load():
    tmpls = {t.name: t for t in list_templates()}
    assert "blank" in tmpls
    assert "python-cli" in tmpls
    assert "skill-starter" in tmpls
    # The bundled template pack should at least ship README + manifest
    py = tmpls["python-cli"]
    assert (py.path / "template.json").is_file()
    assert (py.path / "README.md").is_file()
    assert py.description and py.display_name


def test_get_template_unknown_returns_none():
    assert get_template("does-not-exist-xyz") is None


def test_apply_template_copies_files(tmp_path: Path):
    py = get_template("python-cli")
    assert py is not None
    dest = tmp_path / "ws"
    dest.mkdir()
    ok = apply_template("python-cli", dest)
    assert ok is True
    assert (dest / "README.md").is_file()
    assert (dest / "pyproject.toml").is_file()
    assert (dest / "src" / "__init__.py").is_file()
    # template.json must NOT be copied into the workspace
    assert not (dest / "template.json").exists()


def test_apply_template_unknown_returns_false(tmp_path: Path):
    dest = tmp_path / "ws"
    dest.mkdir()
    assert apply_template("nope", dest) is False


def test_apply_template_skill_starter(tmp_path: Path):
    dest = tmp_path / "ws"
    dest.mkdir()
    assert apply_template("skill-starter", dest) is True
    skill_md = dest / "skills" / "hello" / "SKILL.md"
    assert skill_md.is_file()
    frontmatter = skill_md.read_text(encoding="utf-8")
    assert "name: hello" in frontmatter


def test_apply_template_overwrites_existing(tmp_path: Path):
    dest = tmp_path / "ws"
    dest.mkdir()
    (dest / "README.md").write_text("OLD", encoding="utf-8")
    apply_template("python-cli", dest)
    assert "OLD" not in (dest / "README.md").read_text(encoding="utf-8")
