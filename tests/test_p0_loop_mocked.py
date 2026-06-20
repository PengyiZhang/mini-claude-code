"""P0 AgentLoop integration test with a mocked Anthropic client.

Exercises the full pipeline (tools dispatch + sandbox + persistence)
without making real API calls.
"""
from __future__ import annotations

import types
from dataclasses import dataclass

import pytest

from mini_cc.core.loop import AgentLoop, ProjectRef
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
    """Replays a script of responses. Each call returns the next response."""
    def __init__(self, script: list):
        self.script = list(script)
        self.calls = []
        outer = self

        class _M:
            def create(self_inner, **kw):
                outer.calls.append(kw)
                if not outer.script:
                    raise RuntimeError("script exhausted")
                return outer.script.pop(0)

        self._m = _M()

    @property
    def messages(self):
        return self._m


def _build_loop(tmp_path, script):
    sandbox = SubprocessSandbox("proj-a", tmp_path / "ws")
    storage = FSStorage(tmp_path / "state")
    ref = ProjectRef(project_id="proj-a", project_root=str(tmp_path / "ws"),
                     sandbox=sandbox, storage=storage,
                     client_factory=lambda: _MockClient(script))
    return AgentLoop(ref, "sess1"), sandbox, storage


def test_loop_runs_bash_then_finishes(tmp_path):
    script = [
        _MockResponse([_Block(type="tool_use", name="bash", id="tu1",
                              input={"command": "echo hi > out.txt"})]),
        _MockResponse([_Block(type="text", text="done")], stop_reason="end_turn"),
    ]
    loop, sandbox, storage = _build_loop(tmp_path, script)

    events = list(loop.run("write hi to out.txt"))
    types_seen = [e["type"] for e in events]

    assert "tool_use" in types_seen
    assert "tool_result" in types_seen
    assert events[-1]["type"] == "done"
    # File was actually written through sandbox
    assert sandbox.read("out.txt").strip() == "hi"
    # Messages persisted
    loaded = storage.load_messages("proj-a", "sess1")
    assert any(m.get("role") == "assistant" for m in loaded)


def test_loop_dispatches_unknown_tool_gracefully(tmp_path):
    script = [
        _MockResponse([_Block(type="tool_use", name="nope", id="tu1", input={})]),
        _MockResponse([_Block(type="text", text="ok")], stop_reason="end_turn"),
    ]
    loop, sandbox, storage = _build_loop(tmp_path, script)
    events = list(loop.run("call unknown tool"))
    tr = next(e for e in events if e["type"] == "tool_result")
    assert "Unknown tool" in tr["content"]


def test_loop_blocks_dangerous_command(tmp_path):
    script = [
        _MockResponse([_Block(type="tool_use", name="bash", id="tu1",
                              input={"command": "rm -rf /"})]),
        _MockResponse([_Block(type="text", text="ok")], stop_reason="end_turn"),
    ]
    loop, sandbox, storage = _build_loop(tmp_path, script)
    events = list(loop.run("delete everything"))
    tr = next(e for e in events if e["type"] == "tool_result")
    assert "blocked" in tr["content"].lower()


def test_loop_persists_and_resumes(tmp_path):
    script1 = [
        _MockResponse([_Block(type="tool_use", name="write_file", id="tu1",
                              input={"path": "f.txt", "content": "hello"})]),
        _MockResponse([_Block(type="text", text="wrote")], stop_reason="end_turn"),
    ]
    loop1, _, storage = _build_loop(tmp_path, script1)
    list(loop1.run("write hello to f.txt"))

    # New loop on same project_id/session_id should resume messages
    script2 = [_MockResponse([_Block(type="text", text="bye")],
                             stop_reason="end_turn")]
    sandbox2 = SubprocessSandbox("proj-a", tmp_path / "ws")
    ref2 = ProjectRef(project_id="proj-a", project_root=str(tmp_path / "ws"),
                      sandbox=sandbox2, storage=storage,
                      client_factory=lambda: _MockClient(script2))
    loop2 = AgentLoop(ref2, "sess1")
    assert len(loop2.messages) > 0  # resumed
    list(loop2.run("bye"))
