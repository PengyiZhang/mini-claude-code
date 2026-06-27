"""F6.1 — Project templates.

A template is a directory under ``mini_cc/templates/<name>/`` whose
contents are copied verbatim into a new project's workspace at creation
time. Each template ships a ``template.json`` manifest (name,
description, optional display_name) plus any number of seed files
(README.md, PROJECT.md, skills/, .mini_cc/, …).

Templates are read-only resources bundled with the package. Operators
can add their own by dropping a directory into ``mini_cc/templates/``
(or extending TEMPLATES_DIR via env var MINI_CC_TEMPLATES_DIR).
"""
from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"


@dataclass
class Template:
    name: str
    description: str
    display_name: str
    path: Path


def templates_dir() -> Path:
    """Root templates directory. Override via MINI_CC_TEMPLATES_DIR for
    out-of-tree template packs (operator-supplied, tests)."""
    override = os.environ.get("MINI_CC_TEMPLATES_DIR")
    return Path(override) if override else TEMPLATES_DIR


def list_templates() -> list[Template]:
    """Enumerate available templates, sorted by name. A directory counts
    as a template if it contains ``template.json``."""
    root = templates_dir()
    if not root.is_dir():
        return []
    out: list[Template] = []
    for child in root.iterdir():
        if not child.is_dir():
            continue
        manifest = child / "template.json"
        if not manifest.is_file():
            continue
        try:
            meta = json.loads(manifest.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        out.append(Template(
            name=child.name,
            description=str(meta.get("description", "")).strip(),
            display_name=str(meta.get("display_name", child.name)).strip(),
            path=child,
        ))
    out.sort(key=lambda t: t.name)
    return out


def get_template(name: str) -> Template | None:
    for t in list_templates():
        if t.name == name:
            return t
    return None


def apply_template(template_name: str, dest_workspace: Path) -> bool:
    """Copy a template's contents into ``dest_workspace`` (which must
    already exist). Returns True on success, False if the template is
    unknown. The manifest itself (template.json) is *not* copied — only
    the seed files the user wants in their workspace.

    Existing files in dest_workspace are overwritten; we don't try to
    merge because templates seed a brand-new project.
    """
    tmpl = get_template(template_name)
    if tmpl is None:
        return False
    for entry in tmpl.path.iterdir():
        if entry.name == "template.json":
            continue
        target = dest_workspace / entry.name
        if entry.is_dir():
            shutil.copytree(entry, target, dirs_exist_ok=True)
        else:
            shutil.copy2(entry, target)
    return True
