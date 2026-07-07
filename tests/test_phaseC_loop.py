"""Phase C: AgentLoop interactive permission integration."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass

import pytest

from mini_cc.core.loop import AgentLoop, ProjectRef
from mini_cc.core.permissions import PermissionInterceptor
from mini_cc.sandbox import SubprocessSandbox
from mini_cc.storage import FSStorage


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
    def __init__(self, script):
        self.script = list(script)
        self.calls = []

        class _Stream:
            def __init__(s, r): s._r = r
            def __enter__(s): return s
            def __exit__(s, *e): return False
            def __iter__(s): return iter(())
            def get_final_message(s): return s._r
            def close(s): pass

        class _M:
            def stream(s, **kw):
                self.calls.append(kw)
                if not self.script:
                    raise RuntimeError("script exhausted")
                return _Stream(self.script.pop(0))

        self._m = _M()

    @property
    def messages(self):
        return self._m


def _build_loop(tmp_path, script, prompt_tools=None, timeout=10):
    sandbox = SubprocessSandbox("proj-x", tmp_path / "ws")
    storage = FSStorage(tmp_path / "state")
    interceptor = PermissionInterceptor(timeout_seconds=timeout)
    # client_factory must return a provider (.stream), not a raw SDK client.
    from mini_cc.core.llm import AnthropicProvider
    client = _MockClient(script)
    ref = ProjectRef(
        project_id="proj-x", project_root=str(tmp_path / "ws"),
        sandbox=sandbox, storage=storage,
        client_factory=lambda: AnthropicProvider(lambda: client),
        permissions=interceptor,
        prompt_tools=set(prompt_tools or []),
    )
    loop = AgentLoop(ref, "sess-x")
    return loop, interceptor


def _collect_events_with_decisioner(loop, interceptor, user_input,
                                     decide_action):
    """Run the loop in a thread; on first permission_request, run
    `decide_action(req)` to set the decision. Collect all events."""
    events: list[dict] = []
    error: list = []

    def _runner():
        try:
            for ev in loop.run(user_input):
                events.append(ev)
                if ev.get("type") == "permission_request":
                    decide_action(ev)
        except Exception as e:
            error.append(e)

    t = threading.Thread(target=_runner)
    t.start()
    t.join(timeout=15)
    assert not t.is_alive(), "loop thread did not finish"
    if error:
        raise error[0]
    return events


def test_allow_decision_runs_tool(tmp_path):
    script = [
        _MockResponse([_Block(type="tool_use", name="bash", id="tu1",
                              input={"command": "echo hi > out.txt"})]),
        _MockResponse([_Block(type="text", text="done")], stop_reason="end_turn"),
    ]
    loop, interceptor = _build_loop(tmp_path, script,
                                     prompt_tools={"bash"})

    def decide(ev):
        interceptor.decide(ev["request_id"], "allow")

    events = _collect_events_with_decisioner(
        loop, interceptor, "write hi", decide)
    types = [e["type"] for e in events]
    # Sequence: tool_use → permission_request → tool_result → done
    assert "permission_request" in types
    perm_idx = types.index("permission_request")
    tool_result_idx = types.index("tool_result")
    assert tool_result_idx > perm_idx
    # The tool actually ran (not denied) — content should not contain
    # the deny marker.
    tr = next(e for e in events if e["type"] == "tool_result")
    assert "[permission" not in tr["content"]
    assert events[-1]["type"] == "done"


def test_deny_decision_returns_message(tmp_path):
    script = [
        _MockResponse([_Block(type="tool_use", name="bash", id="tu1",
                              input={"command": "rm -rf build"})]),
        _MockResponse([_Block(type="text", text="ok")], stop_reason="end_turn"),
    ]
    loop, interceptor = _build_loop(tmp_path, script,
                                     prompt_tools={"bash"})

    def decide(ev):
        interceptor.decide(ev["request_id"], "deny",
                           message="user said no")

    events = _collect_events_with_decisioner(
        loop, interceptor, "delete stuff", decide)
    tr = next(e for e in events if e["type"] == "tool_result")
    assert tr["content"] == "user said no"
    # The assistant turn ends normally (deny is a tool_result, then
    # the model gets a chance to respond; here script ends with text).


def test_timeout_returns_timed_out_message(tmp_path):
    script = [
        _MockResponse([_Block(type="tool_use", name="bash", id="tu1",
                              input={"command": "ls"})]),
        _MockResponse([_Block(type="text", text="ok")], stop_reason="end_turn"),
    ]
    loop, interceptor = _build_loop(tmp_path, script,
                                     prompt_tools={"bash"},
                                     timeout=1)
    # No decide_action — let it time out.
    events = _collect_events_with_decisioner(
        loop, interceptor, "ls", decide_action=lambda ev: None)
    tr = next(e for e in events if e["type"] == "tool_result")
    assert tr["content"] == "[permission timed out]"


def test_no_prompt_for_unlisted_tool(tmp_path):
    """Tools not in prompt_tools go through the normal path; no
    permission_request event is emitted."""
    script = [
        _MockResponse([_Block(type="tool_use", name="bash", id="tu1",
                              input={"command": "echo hi > out.txt"})]),
        _MockResponse([_Block(type="text", text="done")], stop_reason="end_turn"),
    ]
    loop, interceptor = _build_loop(tmp_path, script,
                                     prompt_tools={"fs_write"})  # not bash
    events = _collect_events_with_decisioner(
        loop, interceptor, "write", decide_action=lambda ev: None)
    types = [e["type"] for e in events]
    assert "permission_request" not in types
    assert "tool_result" in types


def test_no_interceptor_no_prompts(tmp_path):
    """If permissions=None on the ref, no prompts happen — today's
    behavior. (Just a sanity check that the code path is gated.)"""
    sandbox = SubprocessSandbox("proj-y", tmp_path / "ws")
    storage = FSStorage(tmp_path / "state")
    script = [
        _MockResponse([_Block(type="tool_use", name="bash", id="tu1",
                              input={"command": "echo hi > out.txt"})]),
        _MockResponse([_Block(type="text", text="done")], stop_reason="end_turn"),
    ]
    from mini_cc.core.llm import AnthropicProvider
    client = _MockClient(script)
    ref = ProjectRef(
        project_id="proj-y", project_root=str(tmp_path / "ws"),
        sandbox=sandbox, storage=storage,
        client_factory=lambda: AnthropicProvider(lambda: client),
        permissions=None,
        prompt_tools={"bash"},
    )
    loop = AgentLoop(ref, "sess-y")
    events = list(loop.run("write"))
    types = [e["type"] for e in events]
    assert "permission_request" not in types


def test_session_stop_unblocks_permission_wait(tmp_path):
    """If loop.stop() fires during a permission wait, the wait bails
    quickly with deny/cancel semantics and the loop exits."""
    script = [
        _MockResponse([_Block(type="tool_use", name="bash", id="tu1",
                              input={"command": "ls"})]),
    ]
    loop, interceptor = _build_loop(tmp_path, script,
                                     prompt_tools={"bash"},
                                     timeout=30)

    events: list[dict] = []

    def _runner():
        for ev in loop.run("ls"):
            events.append(ev)
            if ev.get("type") == "permission_request":
                # Stop the loop from another thread shortly.
                threading.Timer(0.2, loop.stop).start()

    t = threading.Thread(target=_runner)
    t.start()
    t.join(timeout=5)
    assert not t.is_alive(), "loop didn't exit after stop()"
    # The permission wait was interrupted; loop ended.
    assert any(e["type"] == "permission_request" for e in events)


def test_assistant_message_emitted_via_on_event(tmp_path):
    """Text-producing turns must fire ``assistant_message`` through the
    ``on_event`` callback (not just via ``yield``). ChannelDispatcher
    lives in the on_event chain, so without this emit, outbound channel
    delivery (e.g. Feishu reply) never fires. Regression for the
    "session shows reply but IM chat doesn't" bug."""
    sandbox = SubprocessSandbox("proj-amsg", tmp_path / "ws")
    storage = FSStorage(tmp_path / "state")
    script = [
        _MockResponse([_Block(type="text", text="hello world")],
                      stop_reason="end_turn"),
    ]
    from mini_cc.core.llm import AnthropicProvider
    client = _MockClient(script)
    ref = ProjectRef(
        project_id="proj-amsg", project_root=str(tmp_path / "ws"),
        sandbox=sandbox, storage=storage,
        client_factory=lambda: AnthropicProvider(lambda: client),
        permissions=None, prompt_tools=set(),
    )
    emitted: list[dict] = []
    loop = AgentLoop(ref, "sess-amsg",
                     on_event=lambda ev: emitted.append(ev))
    # Drain the generator so the run actually executes.
    list(loop.run("hi"))
    amsgs = [e for e in emitted if e.get("type") == "assistant_message"]
    assert len(amsgs) == 1, f"expected 1 assistant_message, got {amsgs}"
    assert amsgs[0].get("text") == "hello world"


def test_assistant_message_not_emitted_for_empty_turn(tmp_path):
    """A turn that produces only tool_use (no assistant text) must not
    emit an empty assistant_message — channel deliver() would send an
    empty IM reply. Guard against regressing into that spam path."""
    sandbox = SubprocessSandbox("proj-empty", tmp_path / "ws")
    storage = FSStorage(tmp_path / "state")
    script = [
        _MockResponse([_Block(type="tool_use", name="bash", id="tu1",
                              input={"command": "echo hi"})]),
        _MockResponse([_Block(type="text", text="")], stop_reason="end_turn"),
    ]
    from mini_cc.core.llm import AnthropicProvider
    client = _MockClient(script)
    ref = ProjectRef(
        project_id="proj-empty", project_root=str(tmp_path / "ws"),
        sandbox=sandbox, storage=storage,
        client_factory=lambda: AnthropicProvider(lambda: client),
        permissions=None, prompt_tools=set(),
    )
    emitted: list[dict] = []
    loop = AgentLoop(ref, "sess-empty",
                     on_event=lambda ev: emitted.append(ev))
    list(loop.run("run"))
    amsgs = [e for e in emitted if e.get("type") == "assistant_message"]
    # Empty-text turn → no assistant_message emitted.
    assert all(e.get("text") for e in amsgs), \
        f"assistant_message with empty text leaked: {amsgs}"
