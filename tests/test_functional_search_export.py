"""F3.1/F3.2: /search and /export command handlers."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from mini_cc.commands.registry import (CommandContext, _cmd_search, _cmd_export,
                                        default_registry)


def _ctx(project_id="p1", session_id="sess_a", storage=None, project=None,
         args=""):
    return CommandContext(
        project_id=project_id, session_id=session_id, tenant_id="t1",
        args=args, project=project, storage=storage)


def _consume(gen):
    """Drain a command generator into (text_concat, has_done)."""
    text = []
    done = False
    for ev in gen:
        if ev.get("type") == "text":
            text.append(ev["text"])
        elif ev.get("type") == "done":
            done = True
    return "\n".join(text), done


# ── /search ──────────────────────────────────────────────────────────────

def test_search_command_finds_matches(tmp_path):
    from mini_cc.storage.fs import FSStorage
    s = FSStorage(tmp_path)
    s.save_messages("p1", "sess_a", [
        {"role": "user", "content": "how do I configure postgres?"},
        {"role": "assistant", "content": "edit settings.py"}])
    s.save_messages("p1", "sess_b", [
        {"role": "user", "content": "totally unrelated"}])
    # Migrated in Plan B.7 — /search now emits a list card. Drain
    # events and check the card payload (not text blob).
    events = list(_cmd_search(_ctx(storage=s, args="postgres")))
    assert any(e.get("type") == "done" for e in events)
    card = next(e for e in events if e.get("type") == "card")
    blob = repr(card["payload"])
    assert "sess_a" in blob
    assert "sess_b" not in blob


def test_search_command_handles_empty_query():
    text, done = _consume(_cmd_search(_ctx(args="")))
    assert "Usage" in text
    assert done


def test_search_command_reports_no_matches(tmp_path):
    from mini_cc.storage.fs import FSStorage
    s = FSStorage(tmp_path)
    s.save_messages("p1", "sess_a", [{"role": "user", "content": "hello"}])
    text, _ = _consume(_cmd_search(_ctx(storage=s, args="missing")))
    assert "No matches" in text


def test_search_registered_in_default_registry():
    reg = default_registry()
    assert reg.resolve("search") is not None
    assert reg.resolve("/search") is not None


# ── /export ──────────────────────────────────────────────────────────────

def test_export_markdown_renders_messages(tmp_path):
    from mini_cc.storage.fs import FSStorage
    ws = tmp_path / "ws"
    ws.mkdir()
    s = FSStorage(tmp_path / "state")
    s.save_messages("p1", "sess_a", [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi there"}])
    proj = SimpleNamespace(workspace=ws, storage=s)
    text, done = _consume(_cmd_export(_ctx(
        storage=s, project=proj, args="md")))
    assert done
    assert "## user" in text
    assert "## assistant" in text
    assert "hello" in text
    # Saved to file?
    fp = ws / ".mini_cc" / "exports" / "sess_a.md"
    assert fp.exists()
    assert "hello" in fp.read_text(encoding="utf-8")


def test_export_json_outputs_raw_messages(tmp_path):
    from mini_cc.storage.fs import FSStorage
    import json
    ws = tmp_path / "ws"
    ws.mkdir()
    s = FSStorage(tmp_path / "state")
    s.save_messages("p1", "sess_a", [{"role": "user", "content": "hi"}])
    proj = SimpleNamespace(workspace=ws, storage=s)
    text, _ = _consume(_cmd_export(_ctx(
        storage=s, project=proj, args="json")))
    parsed = json.loads(text.split("```json\n", 1)[1].split("\n```", 1)[0])
    assert parsed[0]["role"] == "user"


def test_export_rejects_unknown_format():
    text, done = _consume(_cmd_export(_ctx(args="pdf")))
    assert "Unknown format" in text
    assert done


def test_export_handles_empty_session(tmp_path):
    from mini_cc.storage.fs import FSStorage
    s = FSStorage(tmp_path)
    text, done = _consume(_cmd_export(_ctx(storage=s)))
    assert "empty" in text.lower()
    assert done


def test_export_markdown_includes_tool_use_blocks(tmp_path):
    from mini_cc.storage.fs import FSStorage
    ws = tmp_path / "ws"
    ws.mkdir()
    s = FSStorage(tmp_path / "state")
    s.save_messages("p1", "sess_a", [
        {"role": "user", "content": "list files"},
        {"role": "assistant", "content": [
            {"type": "text", "text": "Sure."},
            {"type": "tool_use", "id": "tu1", "name": "bash",
             "input": {"command": "ls"}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "tu1",
             "content": "file1.txt\nfile2.txt"}]}])
    proj = SimpleNamespace(workspace=ws, storage=s)
    text, _ = _consume(_cmd_export(_ctx(storage=s, project=proj)))
    assert "tool_use `bash`" in text
    assert "ls" in text
    assert "tool_result" in text
    assert "file1.txt" in text


def test_export_registered_in_default_registry():
    reg = default_registry()
    assert reg.resolve("export") is not None
