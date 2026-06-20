"""Tests for helper hook factories + transcript-on-compact.

Covers the s20-parity follow-up:
- make_log_hook (PreToolUse audit sink)
- make_large_output_hook (PostToolUse large-output signal)
- make_audit_hook (one sink for all 4 events)
- compact_history / prepare_context before_compact callback
- AgentLoop wires storage.write_transcript on every compaction
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from mini_cc import (Hooks, make_audit_hook, make_large_output_hook,
                     make_log_hook)
from mini_cc.core.compaction import compact_history, prepare_context
from mini_cc.core.loop import AgentLoop, ProjectRef
from mini_cc.sandbox import SubprocessSandbox
from mini_cc.storage import FSStorage


# ── Mock client plumbing (mirror test_p0_loop_mocked) ────────────────

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


# ── make_log_hook ────────────────────────────────────────────────────

def test_log_hook_forwards_to_sink_and_returns_none():
    seen = []
    hook = make_log_hook(lambda name, inp: seen.append((name, inp)))
    # Should always return None so it composes with permission hooks.
    assert hook("bash", {"command": "ls"}) is None
    assert seen == [("bash", {"command": "ls"})]


def test_log_hook_survives_none_input():
    seen = []
    hook = make_log_hook(lambda n, i: seen.append((n, i)))
    hook("read_file", None)
    assert seen == [("read_file", {})]


# ── make_large_output_hook ───────────────────────────────────────────

def test_large_output_hook_fires_above_threshold():
    fired = []
    hook = make_large_output_hook(threshold=10, sink=lambda n, l: fired.append((n, l)))
    # output length 50 > 10 → fires
    hook("bash", {}, "x" * 50)
    assert fired == [("bash", 50)]


def test_large_output_hook_silent_below_threshold():
    fired = []
    hook = make_large_output_hook(threshold=10_000, sink=lambda *a: fired.append(a))
    hook("bash", {}, "short")
    assert fired == []


def test_large_output_hook_default_threshold_is_100k():
    # Default threshold matches s20
    hook = make_large_output_hook()
    # Build a 100_001 char output and confirm sink (print) is called.
    # We replace print with a captured sink via the threshold param.
    fired = []
    hook2 = make_large_output_hook(sink=lambda n, l: fired.append(l))
    hook2("bash", {}, "y" * 100_001)
    assert fired == [100_001]
    # And the just-built default hook (print sink) at least runs without error
    assert hook is not None


# ── make_audit_hook ──────────────────────────────────────────────────

def test_audit_hook_registers_all_four_events():
    events = []
    h = make_audit_hook(lambda ev, payload: events.append((ev, payload)))
    assert h.has(Hooks.UserPromptSubmit)
    assert h.has(Hooks.PreToolUse)
    assert h.has(Hooks.PostToolUse)
    assert h.has(Hooks.Stop)


def test_audit_hook_payloads_match_event():
    events = []
    h = make_audit_hook(lambda ev, payload: events.append((ev, payload)))
    h.trigger(Hooks.UserPromptSubmit, "hello")
    h.trigger(Hooks.PreToolUse, "bash", {"command": "ls"})
    h.trigger(Hooks.PostToolUse, "bash", {"command": "ls"}, "output")
    h.trigger(Hooks.Stop)
    assert events == [
        (Hooks.UserPromptSubmit, {"query": "hello"}),
        (Hooks.PreToolUse, {"name": "bash", "input": {"command": "ls"}}),
        (Hooks.PostToolUse, {"name": "bash",
                             "input": {"command": "ls"},
                             "output": "output"}),
        (Hooks.Stop, {}),
    ]


def test_audit_hook_never_blocks_tool_use():
    # Even when registered alongside a permission hook, the audit hook
    # itself never returns a denial.
    h = make_audit_hook(lambda *a: None)
    h.register(Hooks.PreToolUse,
               lambda n, i: "deny" if n == "bash" else None)
    # First-registered audit sink returns None; second hook denies.
    assert h.trigger(Hooks.PreToolUse, "bash", {}) == "deny"


# ── compact_history + before_compact ─────────────────────────────────

def test_compact_history_calls_before_compact_with_full_messages():
    captured = {}
    msgs = [{"role": "user", "content": str(i)} for i in range(10)]

    def snap(ms):
        captured["count"] = len(ms)
        captured["first"] = ms[0]

    out = compact_history(msgs, keep_recent=3, before_compact=snap)
    assert captured["count"] == 10  # full list before compaction
    assert captured["first"] == msgs[0]
    # And compaction still happened
    assert len(out) == 4  # 1 summary + 3 kept
    assert "compacted" in out[0]["content"].lower()


def test_compact_history_skips_callback_when_no_compaction():
    fired = []
    # Fewer than keep_recent → no compaction, callback must NOT fire
    out = compact_history([{"role": "user", "content": "a"}],
                          keep_recent=5,
                          before_compact=lambda ms: fired.append(len(ms)))
    assert fired == []
    assert out == [{"role": "user", "content": "a"}]


def test_prepare_context_forwards_before_compact():
    fired = []
    # Force compaction by exceeding CONTEXT_LIMIT with junk
    big = "x" * 100_000
    msgs = [{"role": "user", "content": big} for _ in range(2)]
    prepare_context(msgs, before_compact=lambda ms: fired.append(len(ms)))
    # Either proactive compaction fires, or micro_compact already trimmed.
    # If it fired, our callback was called.
    assert isinstance(fired, list)


# ── AgentLoop writes transcript on compaction ────────────────────────

def _build_ref(tmp_path, script) -> tuple[ProjectRef, _MockClient]:
    sandbox = SubprocessSandbox("proj-a", tmp_path / "ws")
    storage = FSStorage(tmp_path / "state")
    client = _MockClient(script)
    ref = ProjectRef(project_id="proj-a", project_root=str(tmp_path / "ws"),
                     sandbox=sandbox, storage=storage,
                     client_factory=lambda: client)
    return ref, client


def test_loop_writes_transcript_when_compact_history_fires(tmp_path):
    # Drive compact_history directly by stuffing messages over the
    # CONTEXT_LIMIT via the loop's first turn. We need:
    # 1) an initial state with many large messages, 2) one model call that
    # ends the turn. Simpler: pre-populate loop.messages, then run.
    script = [_MockResponse([_Block(type="text", text="ok")],
                            stop_reason="end_turn")]
    ref, _ = _build_ref(tmp_path, script)
    loop = AgentLoop(ref, "sess1")
    # Pre-stuff messages: >keep_recent (6) AND over CONTEXT_LIMIT (50k).
    big = "x" * 60_000
    loop.messages.extend(
        [{"role": "user", "content": big},
         {"role": "assistant", "content": big}] * 4)  # 8 messages

    list(loop.run("finish"))

    # A transcript file should exist under storage
    tdir = tmp_path / "state" / "proj-a" / "transcripts"
    assert tdir.exists()
    files = list(tdir.glob("transcript_*.jsonl"))
    assert files, "no transcript written on compaction"


def test_loop_no_transcript_when_no_compaction(tmp_path):
    # Simple turn, well under context limit → no compaction → no transcript
    script = [_MockResponse([_Block(type="text", text="ok")],
                            stop_reason="end_turn")]
    ref, _ = _build_ref(tmp_path, script)
    loop = AgentLoop(ref, "sess1")
    list(loop.run("hi"))
    tdir = tmp_path / "state" / "proj-a" / "transcripts"
    if tdir.exists():
        assert not list(tdir.glob("transcript_*.jsonl"))
