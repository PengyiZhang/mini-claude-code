"""Regression: empty-state branches must emit cards, not text.

Caught during Stage E browser verification: most slash command handlers
were migrated to cards for the populated case but the empty-state
branches still yielded the old text. Browser verification showed
`/bg`, `/skills`, `/tasks`, `/mcp`, `/workflow`, `/search` returning
text fallbacks when the project had no entities of that kind.

Additionally `/loop` crashed with ``AttributeError: 'Project' object
has no attribute 'wakeups'`` because the handler accessed
``project.wakeups`` directly instead of ``getattr(project, "wakeups", None)``
— the runtime Project class doesn't expose ``wakeups`` as an attribute.
"""
from types import SimpleNamespace
from mini_cc.commands import default_registry
from mini_cc.commands.registry import CommandContext


def _ctx(project, args=""):
    storage = getattr(project, "storage", None)
    return CommandContext(
        project_id="e2e_proj", session_id="sess_x",
        tenant_id="e2e", args=args, project=project,
        storage=storage,
    )


def _run(reg, name, project, args=""):
    cmd = reg.resolve(name)
    return list(cmd.handler(_ctx(project, args)))


def _empty_project():
    """Project with all subsystems present but empty."""
    return SimpleNamespace(
        tenant_id="e2e",
        storage=SimpleNamespace(
            load_tasks=lambda pid: [],
            search_messages=lambda pid, q, limit=20: [],
        ),
        # /bg
        background=SimpleNamespace(list_tasks=lambda: []),
        # /skills
        skills_loader=SimpleNamespace(
            scan=lambda: None,
            registry={},
        ),
        # /tasks (uses ctx.storage)
        # /mcp
        mcp_pool=SimpleNamespace(
            list_connected=lambda: [],
            available_servers=lambda: [],
            list_attempts=lambda: {},
            _clients={},
        ),
        # /workflow
        active_workflow=None,
        # /agents
        teams=SimpleNamespace(
            list_alive=lambda: [],
            list_recently_stopped=lambda: [],
            bus=SimpleNamespace(peek_inbox=lambda who: []),
        ),
        # /loop
        scheduler=SimpleNamespace(list_jobs=lambda: []),
        # /permissions
        sandbox=SimpleNamespace(policy=SimpleNamespace(
            blocked=[], allowed_git=set(), allowed_env=set(),
        )),
        # /cost — None means "not configured", which we test separately
    )


def test_bg_empty_state_emits_card():
    reg = default_registry()
    events = _run(reg, "bg", _empty_project())
    cards = [e for e in events if e.get("type") == "card"]
    assert len(cards) == 1
    assert cards[0]["variant"] == "list"
    assert cards[0]["payload"]["items"] == []


def test_skills_empty_state_emits_card():
    reg = default_registry()
    events = _run(reg, "skills", _empty_project())
    cards = [e for e in events if e.get("type") == "card"]
    assert len(cards) == 1
    assert cards[0]["variant"] == "list"
    assert cards[0]["payload"]["items"] == []


def test_tasks_empty_state_emits_card():
    reg = default_registry()
    p = _empty_project()
    events = _run(reg, "tasks", p)
    cards = [e for e in events if e.get("type") == "card"]
    assert len(cards) == 1
    assert cards[0]["variant"] == "list"
    assert cards[0]["payload"]["items"] == []


def test_mcp_empty_state_emits_card():
    reg = default_registry()
    events = _run(reg, "mcp", _empty_project())
    cards = [e for e in events if e.get("type") == "card"]
    assert len(cards) == 1
    assert cards[0]["variant"] == "list"
    assert cards[0]["payload"]["items"] == []


def test_workflow_empty_state_emits_card():
    reg = default_registry()
    events = _run(reg, "workflow", _empty_project())
    cards = [e for e in events if e.get("type") == "card"]
    assert len(cards) == 1
    assert cards[0]["variant"] == "list"
    assert cards[0]["payload"]["items"] == []


def test_loop_empty_state_emits_card_no_crash():
    """Loop must not crash on Project without `wakeups` attribute."""
    reg = default_registry()
    events = _run(reg, "loop", _empty_project())
    errors = [e for e in events if e.get("type") == "error"]
    assert not errors, f"loop crashed: {errors}"
    cards = [e for e in events if e.get("type") == "card"]
    assert len(cards) == 1
    assert cards[0]["variant"] == "list"
    assert cards[0]["payload"]["items"] == []


def test_search_no_args_emits_card_with_empty_hint():
    reg = default_registry()
    events = _run(reg, "search", _empty_project(), args="")
    cards = [e for e in events if e.get("type") == "card"]
    assert len(cards) == 1
    assert cards[0]["payload"]["items"] == []


def test_search_no_matches_emits_card():
    reg = default_registry()
    p = _empty_project()
    # /search uses ctx.storage; pass via ctx
    p.storage = SimpleNamespace(
        search_messages=lambda pid, q, limit=20: [],
    )
    cmd = reg.resolve("search")
    events = list(cmd.handler(CommandContext(
        project_id="e2e_proj", session_id="sess_x", tenant_id="e2e",
        args="nothing-matches-this", project=p,
        storage=p.storage,
    )))
    cards = [e for e in events if e.get("type") == "card"]
    assert len(cards) == 1
    assert cards[0]["payload"]["items"] == []


def test_cost_unconfigured_emits_card_with_warning():
    """When project.metrics is None, /cost should emit a warning card,
    not a text fallback."""
    reg = default_registry()
    p = _empty_project()
    p.metrics = None
    events = _run(reg, "cost", p)
    cards = [e for e in events if e.get("type") == "card"]
    assert len(cards) == 1
    assert cards[0]["status"] == "warning"
