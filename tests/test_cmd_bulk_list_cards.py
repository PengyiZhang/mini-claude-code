"""Bulk tests for /logs /tasks /skills /tools /mcp /search list cards."""
from __future__ import annotations

import os
from types import SimpleNamespace

from mini_cc.commands import default_registry
from mini_cc.commands.registry import CommandContext


def _run(name, **ctx_kwargs):
    reg = default_registry()
    cmd = reg.resolve(name)
    ctx = CommandContext(project_id="p", session_id="s", tenant_id="t",
                         **ctx_kwargs)
    return list(cmd.handler(ctx))


def _card(events):
    cards = [e for e in events if e.get("type") == "card"]
    return cards[0] if cards else None


# ── /skills ─────────────────────────────────────────────────────────────

def _skill(name, desc=""):
    return SimpleNamespace(name=name, description=desc)


def test_skills_yields_list_card():
    loader = SimpleNamespace(
        scan=lambda: None,
        registry={"fs": _skill("fs", "File system reads"),
                   "bash": _skill("bash", "Run shell commands")},
    )
    project = SimpleNamespace(skills_loader=loader)
    events = _run("skills", project=project)
    card = _card(events)
    assert card is not None
    assert card["id"] == "skills"
    assert card["variant"] == "list"
    items = card["payload"]["items"]
    assert {it["title"] for it in items} == {"fs", "bash"}


def test_skills_card_subtitle_is_first_description_line():
    loader = SimpleNamespace(
        scan=lambda: None,
        registry={"fs": _skill("fs", "File system reads")},
    )
    events = _run("skills", project=SimpleNamespace(skills_loader=loader))
    card = _card(events)
    item = card["payload"]["items"][0]
    assert "File system reads" in (item.get("subtitle") or "")


def test_skills_no_loader_yields_error():
    events = _run("skills", project=SimpleNamespace(skills_loader=None))
    assert any(e.get("type") == "error" for e in events)


def test_skills_no_skills_yields_empty_state_card():
    loader = SimpleNamespace(scan=lambda: None, registry={})
    events = _run("skills", project=SimpleNamespace(skills_loader=loader))
    card = _card(events)
    assert card is not None
    assert card["payload"]["items"] == []
    assert card["payload"].get("empty_hint")


# ── /tools ──────────────────────────────────────────────────────────────

def test_tools_yields_list_card_with_builtin_tag():
    """Each tool row is tagged builtin or mcp so users can tell where
    a tool came from at a glance."""
    # The builtin_tools() function is loaded inside the handler — we
    # don't need to mock it. The project has no MCP pool so the only
    # rows will be builtin.
    events = _run("tools", project=SimpleNamespace(mcp_pool=None))
    card = _card(events)
    assert card is not None
    assert card["id"] == "tools"
    assert card["variant"] == "list"
    items = card["payload"]["items"]
    assert len(items) > 0
    # All rows should be tagged builtin (no MCP pool wired).
    for it in items:
        badge_texts = [b["text"].lower() for b in it["badges"]]
        assert "builtin" in badge_texts


def test_tools_card_summary_counts_builtin_and_mcp():
    events = _run("tools", project=SimpleNamespace(mcp_pool=None))
    card = _card(events)
    summary = card["payload"]["summary"] or ""
    assert "builtin" in summary.lower()


# ── /tasks ──────────────────────────────────────────────────────────────

def _task(id="t1", status="pending", subject="thing", owner=None, worktree=None):
    return SimpleNamespace(id=id, status=status, subject=subject,
                            owner=owner, worktree=worktree)


def test_tasks_yields_list_card():
    storage = SimpleNamespace(load_tasks=lambda pid: [_task(), _task("t2", "done")])
    events = _run("tasks", project=SimpleNamespace(), storage=storage)
    card = _card(events)
    assert card is not None
    assert card["id"] == "tasks"
    assert card["variant"] == "list"
    items = card["payload"]["items"]
    assert len(items) == 2


def test_tasks_card_status_badge_tone():
    storage = SimpleNamespace(
        load_tasks=lambda pid: [_task("t1", "pending"),
                                 _task("t2", "done"),
                                 _task("t3", "blocked")],
    )
    events = _run("tasks", project=SimpleNamespace(), storage=storage)
    card = _card(events)
    items = {it["title"]: it for it in card["payload"]["items"]}
    # Pending carries a neutral/warn tone; done carries ok.
    done_badge = next(b for b in items["t2"]["badges"]
                       if "done" in b["text"].lower() or "complete" in b["text"].lower())
    assert done_badge["tone"] == "ok"


def test_tasks_no_tasks_yields_empty_state_card():
    storage = SimpleNamespace(load_tasks=lambda pid: [])
    events = _run("tasks", project=SimpleNamespace(), storage=storage)
    card = _card(events)
    assert card is not None
    assert card["payload"]["items"] == []
    assert card["payload"].get("empty_hint")


# ── /logs ───────────────────────────────────────────────────────────────

def test_logs_no_dir_yields_text_marker(tmp_path, monkeypatch):
    """When the logs directory doesn't exist the command should yield a
    text marker, not an empty card."""
    from pathlib import Path
    import mini_cc.commands.builtin.project_cmds as logs_mod
    # Real layout: <pkg_root>/commands/builtin/project_cmds.py with
    # logs at <pkg_root>/logs. Mirror that so the handler's
    # parent.parent lookup lands where we expect.
    fake_pkg = tmp_path / "pkg"
    fake_cmds = fake_pkg / "commands"
    fake_cmds.mkdir(parents=True)
    (fake_cmds / "project_cmds.py").write_text("")
    # pkg/logs deliberately NOT created
    monkeypatch.setattr(logs_mod, "__file__", str(fake_cmds / "project_cmds.py"))
    events = _run("logs", project=None)
    card = _card(events)
    assert card is None


def test_logs_with_files_yields_card(tmp_path, monkeypatch):
    from pathlib import Path
    import mini_cc.commands.builtin.project_cmds as logs_mod
    fake_pkg = tmp_path / "pkg"
    fake_cmds = fake_pkg / "commands"
    fake_cmds.mkdir(parents=True)
    (fake_cmds / "project_cmds.py").write_text("")
    logs = fake_pkg / "logs"
    logs.mkdir()
    (logs / "build.md").write_text("hello")
    (logs / "error.log").write_text("err")
    monkeypatch.setattr(logs_mod, "__file__", str(fake_cmds / "project_cmds.py"))
    events = _run("logs", project=None)
    card = _card(events)
    assert card is not None
    assert card["id"] == "logs"
    items = card["payload"]["items"]
    titles = {it["title"] for it in items}
    assert "build.md" in titles and "error.log" in titles


# ── /mcp ────────────────────────────────────────────────────────────────

def test_mcp_no_servers_yields_empty_state_card():
    pool = SimpleNamespace(
        _clients={},
        list_attempts=lambda: {},
        available_servers=lambda: [],
    )
    events = _run("mcp", project=SimpleNamespace(mcp_pool=pool))
    card = _card(events)
    assert card is not None
    assert card["payload"]["items"] == []
    assert card["payload"].get("empty_hint")


def test_mcp_with_connected_server_yields_card():
    fake_client = SimpleNamespace(tools=["t1", "t2"])
    pool = SimpleNamespace(
        _clients={"srv1": fake_client},
        list_attempts=lambda: {},
        available_servers=lambda: ["srv1"],
    )
    events = _run("mcp", project=SimpleNamespace(mcp_pool=pool))
    card = _card(events)
    assert card is not None
    assert card["id"] == "mcp"
    items = card["payload"]["items"]
    assert items[0]["title"] == "srv1"
    badge_texts = [b["text"].lower() for b in items[0]["badges"]]
    assert "connected" in badge_texts


# ── /search ─────────────────────────────────────────────────────────────

def _search_hit(sid="s1", role="user", idx=0, snippet="hello world"):
    return SimpleNamespace(session_id=sid, role=role,
                            message_index=idx, snippet=snippet)


def test_search_no_query_yields_empty_state_card():
    events = _run("search", project=SimpleNamespace(), args="", storage=SimpleNamespace())
    card = _card(events)
    assert card is not None
    assert card["payload"]["items"] == []
    assert card["payload"].get("empty_hint")


def test_search_with_hits_yields_card():
    storage = SimpleNamespace(
        search_messages=lambda pid, q, limit=20: [
            _search_hit("s1", "user", 0, "found login flow"),
            _search_hit("s2", "assistant", 3, "auth pattern"),
        ],
    )
    events = _run("search", project=SimpleNamespace(), args="login",
                   storage=storage)
    card = _card(events)
    assert card is not None
    assert card["id"] == "search"
    items = card["payload"]["items"]
    assert len(items) == 2
    assert items[0]["title"] == "s1"


def test_search_card_each_hit_has_resume_expandable():
    storage = SimpleNamespace(
        search_messages=lambda pid, q, limit=20: [_search_hit("s1")],
    )
    events = _run("search", project=SimpleNamespace(), args="x",
                   storage=storage)
    card = _card(events)
    item = card["payload"]["items"][0]
    assert item["expandable_command"] == "/resume s1"


def test_search_no_hits_yields_empty_state_card():
    storage = SimpleNamespace(
        search_messages=lambda pid, q, limit=20: [],
    )
    events = _run("search", project=SimpleNamespace(), args="zzz",
                   storage=storage)
    card = _card(events)
    assert card is not None
    assert card["payload"]["items"] == []
    assert card["payload"].get("empty_hint")
