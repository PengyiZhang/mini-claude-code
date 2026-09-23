"""M4-7: parallel_safe 白名单 + 同轮 tool_use 并行执行。"""
from __future__ import annotations

import time
from dataclasses import dataclass

import pytest

from mini_cc.core.loop import AgentLoop, ProjectRef
from mini_cc.core.llm import AnthropicProvider
from mini_cc.sandbox import SubprocessSandbox
from mini_cc.storage import FSStorage
from mini_cc.tools.base import FunctionTool
from mini_cc.tools.fs import READ_TOOL, GLOB_TOOL, GREP_TOOL
from mini_cc.tools.web import WEB_FETCH_TOOL
from mini_cc.tools.websearch import WEB_SEARCH_TOOL


def _slow_tool(name, seconds, parallel_safe=False, fn=None):
    return FunctionTool(
        name=name, description=f"slow {name}",
        input_schema={"type": "object", "properties": {}, "required": []},
        fn=fn or (lambda ctx, args: (time.sleep(seconds), f"{name} done")[1]),
        parallel_safe=parallel_safe)


# ── Mock client skeleton (copied from tests/test_p0_loop_mocked.py) ─────────

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

        class _Stream:
            """Context-manager mock matching the SDK's messages.stream()."""
            def __init__(self_inner, response):
                self_inner._response = response
            def __enter__(self_inner):
                return self_inner
            def __exit__(self_inner, *exc):
                return False
            def __iter__(self_inner):
                return iter(())  # no streaming events; loop body just polls _stop
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


def _echo_factory(tools):
    """client_factory script: response 1 = one tool_use block per tool,
    response 2 = a single text block 'done'."""
    script = [
        _MockResponse([_Block(type="tool_use", name=t.name, id=f"tu-{i}",
                              input={}) for i, t in enumerate(tools)]),
        _MockResponse([_Block(type="text", text="done")], stop_reason="end_turn"),
    ]
    client = _MockClient(script)
    return lambda: AnthropicProvider(lambda: client)


def _build_loop(tmp_path, tools):
    sandbox = SubprocessSandbox("proj-p", tmp_path / "ws")
    storage = FSStorage(tmp_path / "state")
    ref = ProjectRef(project_id="proj-p", project_root=str(tmp_path / "ws"),
                     sandbox=sandbox, storage=storage,
                     client_factory=_echo_factory(tools))
    # Frozen tool list — AgentLoop uses it as-is, no MCP rebuild.
    return AgentLoop(ref, "sess1", tools=tools)


# ── Tests ────────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True, scope="module")
def _warm_first_run(tmp_path_factory):
    """Pay the first-run()-in-process cost (lazy imports inside
    _run_impl, e.g. server.tracing) before any wall-clock measurement —
    it otherwise lands on whichever test happens to run first."""
    tmp = tmp_path_factory.mktemp("warmup")
    tools = [_slow_tool("warm_a", 0.0), _slow_tool("warm_b", 0.0)]
    list(_build_loop(tmp, tools).run("warm"))

def test_parallel_safe_flag():
    plain = FunctionTool(name="x", description="x",
                         input_schema={"type": "object", "properties": {}},
                         fn=lambda ctx, args: "ok")
    assert plain.parallel_safe is False  # 默认关闭 — 保守并行
    for tool in (READ_TOOL, GLOB_TOOL, GREP_TOOL,
                 WEB_FETCH_TOOL, WEB_SEARCH_TOOL):
        assert tool.parallel_safe is True, tool.name


def test_parallel_batch_faster_than_serial(tmp_path, monkeypatch):
    monkeypatch.delenv("MINI_CC_TOOL_WORKERS", raising=False)
    tools = [_slow_tool("slow_a", 0.3, parallel_safe=True),
             _slow_tool("slow_b", 0.3, parallel_safe=True)]
    loop = _build_loop(tmp_path, tools)

    t0 = time.monotonic()
    events = list(loop.run("go"))
    elapsed = time.monotonic() - t0

    uses = [e for e in events if e["type"] == "tool_use"]
    results = [e for e in events if e["type"] == "tool_result"]
    assert len(uses) == 2
    assert len(results) == 2
    assert {r["content"] for r in results} == {"slow_a done", "slow_b done"}
    assert elapsed < 0.55  # 串行 ≥0.6s；并行 ≈0.3s + 开销


def test_events_all_uses_before_results_in_batch(tmp_path, monkeypatch):
    monkeypatch.delenv("MINI_CC_TOOL_WORKERS", raising=False)
    tools = [_slow_tool("slow_a", 0.05, parallel_safe=True),
             _slow_tool("slow_b", 0.05, parallel_safe=True)]
    loop = _build_loop(tmp_path, tools)

    events = list(loop.run("go"))
    kinds = [e["type"] for e in events]
    first_result = kinds.index("tool_result")
    # 批内所有 tool_use 先于任何 tool_result（两阶段执行的事件形状）。
    assert all(k != "tool_use" for k in kinds[first_result:])


def test_workers_env_disables_parallel(tmp_path, monkeypatch):
    monkeypatch.setenv("MINI_CC_TOOL_WORKERS", "1")
    tools = [_slow_tool("slow_a", 0.3, parallel_safe=True),
             _slow_tool("slow_b", 0.3, parallel_safe=True)]
    loop = _build_loop(tmp_path, tools)

    t0 = time.monotonic()
    events = list(loop.run("go"))
    elapsed = time.monotonic() - t0

    results = [e for e in events if e["type"] == "tool_result"]
    assert len(results) == 2
    assert elapsed >= 0.55  # 串行回退：0.3 + 0.3


def test_unflagged_tool_stays_serial(tmp_path, monkeypatch):
    monkeypatch.delenv("MINI_CC_TOOL_WORKERS", raising=False)
    tools = [_slow_tool("slow_a", 0.3),
             _slow_tool("slow_b", 0.3)]
    loop = _build_loop(tmp_path, tools)

    t0 = time.monotonic()
    events = list(loop.run("go"))
    elapsed = time.monotonic() - t0

    results = [e for e in events if e["type"] == "tool_result"]
    assert len(results) == 2
    assert elapsed >= 0.55  # 未加白名单 → 串行


# ── PostToolUse regression: hook must fire on ALL non-denied paths ───────────
# 3b8d592 introduced early returns on the bg-offload and unknown-tool
# paths that skipped the PostToolUse trigger. Original semantics:
# denied → no PostToolUse; everything else → PostToolUse(name, input, output).

class _HooksStub:
    """Minimal Hooks stand-in. Records every trigger(event, *args) call;
    PreToolUse returns pre_denial (None → allow), matching the
    deny-by-non-None-return protocol of real Hooks.trigger."""

    def __init__(self, pre_denial=None):
        self.calls = []
        self.pre_denial = pre_denial

    def trigger(self, event, *args):
        self.calls.append((event, args))
        return self.pre_denial if event == "PreToolUse" else None


class _BgStub:
    """BackgroundScheduler stand-in: start() records the call and
    returns a fixed bg_id."""

    def __init__(self, bg_id="bg-7"):
        self.bg_id = bg_id
        self.started = []

    def start(self, ctx, tools, name, tool_input, tool_use_id):
        self.started.append((name, tool_input, tool_use_id))
        return self.bg_id


def test_run_one_fires_posttooluse_for_unknown_tool(tmp_path):
    loop = _build_loop(tmp_path, [])
    stub = _HooksStub()
    loop.hooks = stub
    ctx = loop._make_ctx()

    out = loop._run_one(ctx, "nope", {}, "t1")

    assert out == "Unknown tool: nope"
    assert ("PostToolUse", ("nope", {}, out)) in stub.calls


def test_run_one_fires_posttooluse_for_background_offload(tmp_path):
    loop = _build_loop(tmp_path, [_slow_tool("bash", 0.0)])
    stub = _HooksStub()
    loop.hooks = stub
    bg = _BgStub("bg-7")
    loop.project.background = bg
    ctx = loop._make_ctx()
    # should_run_background requires tool "bash" + run_in_background
    # truthy (or a SLOW_KEYWORDS command match).
    tool_input = {"command": "sleep 5", "run_in_background": True}

    out = loop._run_one(ctx, "bash", tool_input, "t2")

    assert "[Background task bg-7 started]" in out
    assert bg.started == [("bash", tool_input, "t2")]
    assert ("PostToolUse", ("bash", tool_input, out)) in stub.calls


def test_run_one_denied_skips_posttooluse(tmp_path):
    loop = _build_loop(tmp_path, [_slow_tool("x", 0.0)])
    stub = _HooksStub(pre_denial="denied!")
    loop.hooks = stub
    ctx = loop._make_ctx()

    out = loop._run_one(ctx, "x", {}, "t3")

    assert out == "denied!"
    assert not [c for c in stub.calls if c[0] == "PostToolUse"]
