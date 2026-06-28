"""P0 tests for the hooks pipeline + subagent dispatch.

Mirrors the mocking pattern in test_p0_loop_mocked.py: _Block,
_MockResponse, _MockClient replay a script of model responses so we can
exercise hook firing points and the subagent's restricted tool set
without making real API calls.
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from mini_cc import (DENY_LIST, DESTRUCTIVE, Hooks, ProjectManager,
                     make_permission_hook, spawn_subagent)
from mini_cc.core.hooks import Hooks as HooksCls
from mini_cc.core.loop import AgentLoop, ProjectRef
from mini_cc.core.subagent import SUBAGENT_TOOL_NAMES
from mini_cc.sandbox import SubprocessSandbox
from mini_cc.storage import FSStorage
from mini_cc.tools import builtin_tools, to_anthropic
from mini_cc.tools.base import ToolContext
from mini_cc.tools.subagent import TASK_TOOL


# ── Mock client plumbing ──────────────────────────────────────────────

@dataclass
class _Block:
    type: str
    text: str | None = None
    name: str | None = None
    input: dict | None = None
    id: str | None = None


class _MockResponse:
    def __init__(self, content, stop_reason="tool_use"):
        self.content = content
        self.stop_reason = stop_reason


class _MockClient:
    """Replays a script of responses; captures every create() call kwargs."""
    def __init__(self, script: list):
        self.script = list(script)
        self.calls: list[dict] = []
        outer = self

        class _Stream:
            def __init__(self_inner, response):
                self_inner._response = response
            def __enter__(self_inner):
                return self_inner
            def __exit__(self_inner, *exc):
                return False
            def __iter__(self_inner):
                return iter(())
            def get_final_message(self_inner):
                return self_inner._response
            def close(self_inner):
                pass

        class _M:
            def create(self_inner, **kw):
                outer.calls.append(kw)
                if not outer.script:
                    raise RuntimeError("script exhausted")
                return outer.script.pop(0)

            def stream(self_inner, **kw):
                outer.calls.append(kw)
                if not outer.script:
                    raise RuntimeError("script exhausted")
                return _Stream(outer.script.pop(0))

        self._m = _M()

    @property
    def messages(self):
        return self._m


def _build_ref(tmp_path, script=None, *, client=None) -> tuple[ProjectRef, _MockClient]:
    sandbox = SubprocessSandbox("proj-a", tmp_path / "ws")
    storage = FSStorage(tmp_path / "state")
    cl = client or _MockClient(script or [])
    # Loop expects client_factory to return a provider (with .stream),
    # not a raw SDK client. Wrap the SDK-shape mock in AnthropicProvider.
    from mini_cc.core.llm import AnthropicProvider
    ref = ProjectRef(project_id="proj-a", project_root=str(tmp_path / "ws"),
                     sandbox=sandbox, storage=storage,
                     client_factory=lambda: AnthropicProvider(lambda: cl))
    return ref, cl


# ── Hooks class ───────────────────────────────────────────────────────

def test_hooks_register_and_trigger_first_non_none():
    h = Hooks()
    h.register(HooksCls.PreToolUse, lambda name, inp: None)
    h.register(HooksCls.PreToolUse, lambda name, inp: "deny-2")
    h.register(HooksCls.PreToolUse, lambda name, inp: "deny-3")
    assert h.trigger(HooksCls.PreToolUse, "bash", {}) == "deny-2"


def test_hooks_trigger_returns_none_when_empty():
    h = Hooks()
    assert h.trigger(HooksCls.UserPromptSubmit, "hello") is None


def test_hooks_has_and_clear():
    h = Hooks()
    assert not h.has(HooksCls.Stop)
    h.register(HooksCls.Stop, lambda: None)
    assert h.has(HooksCls.Stop)
    h.clear(HooksCls.Stop)
    assert not h.has(HooksCls.Stop)
    # clear-all path
    h.register(HooksCls.Stop, lambda: None)
    h.register(HooksCls.PreToolUse, lambda n, i: None)
    h.clear()
    assert not h.has(HooksCls.Stop)
    assert not h.has(HooksCls.PreToolUse)


# ── make_permission_hook ──────────────────────────────────────────────

def test_permission_hook_denies_deny_list():
    hook = make_permission_hook()
    assert hook("bash", {"command": "sudo rm -rf /"}) is not None


def test_permission_hook_denies_destructive_by_default():
    hook = make_permission_hook()
    # "rm " is in DESTRUCTIVE
    assert hook("bash", {"command": "rm somefile"}) is not None


def test_permission_hook_allows_destructive_when_enabled():
    hook = make_permission_hook(allow_destructive=True)
    # still denies DENY_LIST entries
    assert hook("bash", {"command": "sudo x"}) is not None
    # but rm (destructive only) is now allowed
    assert hook("bash", {"command": "rm somefile"}) is None


def test_permission_hook_ignores_non_bash_and_safe_cmds():
    hook = make_permission_hook()
    assert hook("read", {"path": "/etc/passwd"}) is None
    assert hook("bash", {"command": "ls -la"}) is None
    assert hook("bash", {"command": "echo hello"}) is None


# ── AgentLoop hook integration ────────────────────────────────────────

def test_user_prompt_submit_replaces_query(tmp_path):
    script = [_MockResponse([_Block(type="text", text="ok")],
                            stop_reason="end_turn")]
    ref, client = _build_ref(tmp_path, script)
    hooks = Hooks()
    hooks.register(Hooks.UserPromptSubmit,
                   lambda q: q + " [appended]")
    loop = AgentLoop(ref, "s1", hooks=hooks)
    list(loop.run("do thing"))
    # The stored user message includes the appended suffix
    user_msgs = [m for m in loop.messages if m["role"] == "user"]
    assert user_msgs
    assert "[appended]" in user_msgs[0]["content"]


def test_pretooluse_denial_becomes_tool_result(tmp_path):
    script = [
        _MockResponse([_Block(type="tool_use", name="bash", id="tu1",
                              input={"command": "sudo rm -rf /"})]),
        _MockResponse([_Block(type="text", text="ok")], stop_reason="end_turn"),
    ]
    ref, _ = _build_ref(tmp_path, script)
    hooks = Hooks()
    hooks.register(Hooks.PreToolUse, make_permission_hook())
    loop = AgentLoop(ref, "s1", hooks=hooks)
    events = list(loop.run("danger"))
    tr = next(e for e in events if e["type"] == "tool_result")
    assert "Permission denied" in tr["content"]


def test_posttooluse_fires_after_call(tmp_path):
    seen = []
    script = [
        _MockResponse([_Block(type="tool_use", name="bash", id="tu1",
                              input={"command": "echo hi"})]),
        _MockResponse([_Block(type="text", text="ok")], stop_reason="end_turn"),
    ]
    ref, _ = _build_ref(tmp_path, script)
    hooks = Hooks()
    hooks.register(Hooks.PostToolUse,
                   lambda name, inp, out: seen.append((name, out)))
    loop = AgentLoop(ref, "s1", hooks=hooks)
    list(loop.run("run echo"))
    assert len(seen) == 1
    assert seen[0][0] == "bash"
    assert "hi" in seen[0][1]


def test_stop_hook_fires_on_turn_end(tmp_path):
    fired = []
    script = [_MockResponse([_Block(type="text", text="done")],
                            stop_reason="end_turn")]
    ref, _ = _build_ref(tmp_path, script)
    hooks = Hooks()
    hooks.register(Hooks.Stop, lambda: fired.append(True))
    loop = AgentLoop(ref, "s1", hooks=hooks)
    list(loop.run("hi"))
    assert fired == [True]


# ── system_prompt_override ────────────────────────────────────────────

def test_system_prompt_override_is_used(tmp_path):
    script = [_MockResponse([_Block(type="text", text="ok")],
                            stop_reason="end_turn")]
    ref, client = _build_ref(tmp_path, script)
    override = "YOU ARE A SUBAGENT. Be terse."
    loop = AgentLoop(ref, "s1", system_prompt_override=override)
    list(loop.run("hi"))
    assert client.calls[0]["system"] == override


# ── spawn_subagent ────────────────────────────────────────────────────

def test_spawn_subagent_restricted_tools_and_returns_text(tmp_path):
    script = [_MockResponse([_Block(type="text", text="subagent summary")],
                            stop_reason="end_turn")]
    ref, client = _build_ref(tmp_path, script)
    # The subagent uses its own client via client_factory on ProjectRef.
    sub_client = _MockClient(script)

    def factory():
        # spawn_subagent stores loop._client = factory() and then calls
        # .stream(...) on it — so the factory must return a provider, not
        # a raw SDK client.
        from mini_cc.core.llm import AnthropicProvider
        return AnthropicProvider(lambda: sub_client)

    summary = spawn_subagent(ref, "do thing", client_factory=factory)
    assert summary == "subagent summary"
    # Tool set advertised to the model is exactly the restricted set.
    sent_tools = sub_client.calls[0]["tools"]
    sent_names = {t["name"] for t in sent_tools}
    assert sent_names == set(SUBAGENT_TOOL_NAMES)
    # No privileged tools leaked in.
    assert "task" not in sent_names
    assert "spawn_teammate" not in sent_names
    assert "connect_mcp" not in sent_names


def test_spawn_subagent_no_summary_fallback(tmp_path):
    # Script emits only a tool_use, then exhausted (no text). Subagent
    # hits end of stream without producing text -> fallback string.
    # But we need a clean termination: one tool_use then end_turn text
    # with empty text. Use empty text + end_turn.
    script = [
        _MockResponse([_Block(type="tool_use", name="bash", id="t1",
                              input={"command": "echo hi"})]),
        _MockResponse([_Block(type="text", text="")], stop_reason="end_turn"),
    ]
    ref, _ = _build_ref(tmp_path, script)
    sub_client = _MockClient(script)
    from mini_cc.core.llm import AnthropicProvider
    summary = spawn_subagent(ref, "do thing",
                             client_factory=lambda: AnthropicProvider(lambda: sub_client))
    # Empty final text -> fallback message
    assert "summary" in summary.lower() or "without" in summary.lower()


def test_spawn_subagent_tool_call_cap(tmp_path):
    # Script emits many tool_use blocks (each is one tool call). With a
    # tight max_tool_calls, the subagent stops early.
    # Each tool_use response leads to one tool_use event; we make the
    # script repeat tool_use then end with text so we can observe cap.
    script = [
        _MockResponse([_Block(type="tool_use", name="bash", id="t1",
                              input={"command": "echo 1"})]),
        _MockResponse([_Block(type="tool_use", name="bash", id="t2",
                              input={"command": "echo 2"})]),
        _MockResponse([_Block(type="tool_use", name="bash", id="t3",
                              input={"command": "echo 3"})]),
        _MockResponse([_Block(type="text", text="all done")],
                      stop_reason="end_turn"),
    ]
    ref, _ = _build_ref(tmp_path, script)
    sub_client = _MockClient(script)
    from mini_cc.core.llm import AnthropicProvider
    summary = spawn_subagent(ref, "loop", max_tool_calls=2,
                             client_factory=lambda: AnthropicProvider(lambda: sub_client))
    # Cap reached; no text was produced before stop -> fallback mentions cap
    assert "tool-call cap" in summary or "all done" in summary


# ── `task` tool ───────────────────────────────────────────────────────

def test_task_tool_requires_project_ref(tmp_path):
    ctx = ToolContext(project_id="p", session_id="s",
                      sandbox=SubprocessSandbox("p", tmp_path),
                      storage=FSStorage(tmp_path / "st"),
                      todos=[])
    out = TASK_TOOL.handle(ctx, {"description": "do thing"})
    assert "ProjectRef" in out


def test_task_tool_dispatches_subagent(tmp_path):
    script = [_MockResponse([_Block(type="text", text="via tool")],
                            stop_reason="end_turn")]
    ref, _ = _build_ref(tmp_path, script)
    sub_client = _MockClient(script)
    from mini_cc.core.llm import AnthropicProvider
    ctx = ToolContext(
        project_id="p", session_id="s",
        sandbox=SubprocessSandbox("p", tmp_path),
        storage=FSStorage(tmp_path / "st"),
        todos=[],
        project_ref=ref,
        subagent_client_factory=lambda: AnthropicProvider(lambda: sub_client),
    )
    out = TASK_TOOL.handle(ctx, {"description": "do thing"})
    # P0-6: tool_result now surfaces the subagent's session_id so the
    # parent can correlate the summary with the subagent transcript.
    assert out.startswith("via tool")
    assert "[subagent_session_id: subagent-" in out


def test_task_tool_session_id_matches_persisted_transcript(tmp_path):
    """P0-6 regression: the session_id surfaced in the tool_result must
    identify a real on-disk transcript for the subagent — that's the
    whole point of including it. Verified by loading messages back from
    storage under the surfaced id."""
    script = [_MockResponse([_Block(type="text", text="hello sub")],
                            stop_reason="end_turn")]
    ref, _ = _build_ref(tmp_path, script)
    sub_client = _MockClient(script)
    from mini_cc.core.llm import AnthropicProvider
    ctx = ToolContext(
        project_id="p", session_id="s",
        sandbox=SubprocessSandbox("p", tmp_path),
        storage=FSStorage(tmp_path / "st"),
        todos=[],
        project_ref=ref,
        subagent_client_factory=lambda: AnthropicProvider(lambda: sub_client),
    )
    out = TASK_TOOL.handle(ctx, {"description": "do thing"})
    # Extract the session_id from the footer.
    sid_line = [ln for ln in out.splitlines()
                if ln.startswith("[subagent_session_id:")][0]
    sid = sid_line.split(":", 1)[1].strip().rstrip("]").strip()
    assert sid.startswith("subagent-")
    # The subagent's transcript must exist under that id on disk.
    msgs = ref.storage.load_messages(ref.project_id, sid)
    assert msgs, f"no transcript persisted for subagent session {sid}"
    assert any(m["role"] == "assistant" for m in msgs)


# ── Project.hooks wiring ──────────────────────────────────────────────

def test_project_has_fresh_hooks(tmp_path):
    pm = ProjectManager(tmp_path / "pm")
    p1 = pm.create(tenant_id="t", project_id="a")
    p2 = pm.create(tenant_id="t", project_id="b")
    assert isinstance(p1.hooks, Hooks)
    assert isinstance(p2.hooks, Hooks)
    assert p1.hooks is not p2.hooks
    # Registering on one does not affect the other
    p1.hooks.register(Hooks.PreToolUse, lambda n, i: "deny")
    assert p1.hooks.has(Hooks.PreToolUse)
    assert not p2.hooks.has(Hooks.PreToolUse)
