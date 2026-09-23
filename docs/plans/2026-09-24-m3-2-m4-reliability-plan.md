# M3-2 + M4 可靠性批次实施计划

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 完成 M3-2（teams 拆分）与 M4-1/2/3/4/7（错误分类、保守并行工具、/readyz、指标补全），每项一个 TDD commit。

**Architecture:** 纯移动重构（teams）+ recovery.py 共享错误分类器供 with_retry 与 loop 复用 + loop 工具执行两阶段化（串行语义不变、parallel_safe 批次进线程池）+ app.py 新增 /readyz（探针 TTL 缓存）+ MetricsRegistry 新指标族四处埋点。

**Tech Stack:** Python 3.11+、pytest、concurrent.futures.ThreadPoolExecutor、FastAPI（TestClient）。

**设计文档:** `docs/plans/2026-09-24-m3-2-m4-reliability-design.md`（本计划是其执行细化）

**约定:**
- 全量测试：`python -m pytest tests/ -q`（Windows bash；基线 1343 全绿）
- 每个任务结束跑全量，绿了才 commit
- commit message 用各任务给定的文本 + Happy/Claude 署名脚注

---

### Task 1: M3-2 — teams/__init__.py 拆分

**Files:**
- Create: `mini_cc/teams/bus.py`、`mini_cc/teams/protocol.py`、`mini_cc/teams/spawner.py`、`mini_cc/teams/convention.py`
- Rewrite: `mini_cc/teams/__init__.py`（1304 行 → <120 行 re-export）
- Test: `tests/test_m3_2_teams_split.py`

**背景（已核实）:**
- `__init__.py` 布局：L1-41 模块 docstring/imports/portalocker 守卫；L44-89 `_format_inbox_as_dialogue`；L91-470 `MessageBus`；L471-521 `ProtocolState`+`ProtocolTracker`；L522-550 `TeammateInfo`；L551-1246 `TeammateSpawner`；L1247-1304 `_CONVENTION_PROMPT`+`convention_prompt`
- 外部引用面（拆分后必须不断裂）：
  - `mini_cc/__init__.py:50` — `from .teams import (MessageBus, ProtocolState, ProtocolTracker, TeammateInfo, TeammateSpawner)`
  - `core/loop.py:28`、`projects/manager.py:28`、`_shim.py:20`、`tools/base.py:18`（TYPE_CHECKING）— `TeammateSpawner`
  - `teams/mentions.py:18` — `from . import MessageBus`（包内相对导入，靠 re-export）
- 已有模块先例：`teams/watcher.py`、`teams/disposition.py`、`teams/mentions.py`

**Step 1: 写失败测试**

`tests/test_m3_2_teams_split.py`：

```python
"""M3-2: teams 包拆分 — 新模块落位 + 包级 re-export 面不变。"""
from __future__ import annotations

import inspect

import mini_cc
from mini_cc import teams


def test_new_module_homes():
    from mini_cc.teams.bus import MessageBus, _format_inbox_as_dialogue
    from mini_cc.teams.protocol import ProtocolState, ProtocolTracker
    from mini_cc.teams.spawner import TeammateInfo, TeammateSpawner
    from mini_cc.teams.convention import convention_prompt
    assert callable(convention_prompt)
    assert MessageBus and ProtocolTracker and TeammateSpawner  # noqa


def test_reexport_identity():
    from mini_cc.teams.bus import MessageBus as Bus
    from mini_cc.teams.protocol import ProtocolTracker as Tracker
    from mini_cc.teams.spawner import TeammateSpawner as Spawner
    assert teams.MessageBus is Bus
    assert teams.ProtocolTracker is Tracker
    assert teams.TeammateSpawner is Spawner
    # 顶层包导出仍然可用（mini_cc/__init__.py:50 的导入路径）
    from mini_cc import (MessageBus, ProtocolState, ProtocolTracker,  # noqa
                         TeammateInfo, TeammateSpawner)
    assert mini_cc.TeammateSpawner is Spawner


def test_init_is_thin():
    src = inspect.getsource(teams)
    assert len(src.splitlines()) < 120, (
        "teams/__init__.py 应只留 re-export，不应再承载实现")
```

**Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_m3_2_teams_split.py -v`
Expected: FAIL（`ModuleNotFoundError: mini_cc.teams.bus`）

**Step 3: 拆分（纯移动，不改行为）**

1. `teams/bus.py`：模块 docstring 一句话（"Per-project JSONL mailboxes + file locking."）+ 从 `__init__.py` **原样搬** L44-89（`_format_inbox_as_dialogue`）与 L91-470（`MessageBus`）。头部 import 起步集：
   ```python
   from __future__ import annotations
   import json, threading, time, uuid
   from contextlib import contextmanager
   from pathlib import Path
   from typing import Callable
   ```
   加 portalocker try/except 守卫块（原 L36-41 原样）。跑测试时按 NameError 增删（`os`/`dataclass` 若用到就补）。
2. `teams/protocol.py`：搬 L471-521（`ProtocolState`、`ProtocolTracker`）。import：`from __future__ import annotations`、`threading`、`from dataclasses import dataclass, field`、`time`。
3. `teams/convention.py`：搬 L1247-1304（`_CONVENTION_PROMPT`、`convention_prompt`），无额外依赖。
4. `teams/spawner.py`：搬 L522-550（`TeammateInfo`）+ L551-1246（`TeammateSpawner`）。头部：
   ```python
   from __future__ import annotations
   import json, os, threading, time, uuid
   from dataclasses import dataclass, field
   from pathlib import Path
   from typing import TYPE_CHECKING, Callable
   from .bus import MessageBus, _format_inbox_as_dialogue
   from .protocol import ProtocolState, ProtocolTracker
   from .convention import convention_prompt
   if TYPE_CHECKING:
       from ..core.loop import AgentLoop
       from ..storage import Task
   ```
   （spawner 内部对这几样的实际使用以搬完后 NameError/未用 import 为准微调——比如 `_format_inbox_as_dialogue` 或 `convention_prompt` 若 spawner 并未引用，就从导入里去掉。）
5. 重写 `teams/__init__.py`：保留原模块 docstring（前 19 行），随后：
   ```python
   from .bus import MessageBus, _format_inbox_as_dialogue
   from .protocol import ProtocolState, ProtocolTracker
   from .spawner import TeammateInfo, TeammateSpawner
   from .convention import convention_prompt

   __all__ = ["MessageBus", "ProtocolState", "ProtocolTracker",
              "TeammateInfo", "TeammateSpawner", "convention_prompt",
              "_format_inbox_as_dialogue"]
   ```
6. 检查 `teams/disposition.py`、`teams/watcher.py` 头部是否还有 `from . import X` 依赖（mentions.py 有，已核实）——re-export 保持后无需改动。

**Step 4: 跑新测试确认通过**

Run: `python -m pytest tests/test_m3_2_teams_split.py -v`
Expected: 3 PASS

**Step 5: 全量测试守护**

Run: `python -m pytest tests/ -q`
Expected: 全绿（1343 + 3 新增）。任何红→修复（通常是漏搬 import 或循环导入；bus/protocol/convention 不得 import spawner）。

**Step 6: Commit**

```bash
git add mini_cc/teams/ tests/test_m3_2_teams_split.py
git commit -m "refactor(m3): split teams/__init__ into bus/protocol/spawner/convention" # + 署名脚注
```

---

### Task 2: M4-1 + M4-2 — 错误分类器（一个 commit）

**Files:**
- Modify: `mini_cc/core/recovery.py`（新增 `ErrorClass`/`classify_error`；改 `with_retry`）
- Modify: `mini_cc/core/loop.py:803-837`（except 路径）
- Test: `tests/test_m4_error_classification.py`

**Step 1: 写失败测试**

```python
"""M4-1/M4-2: classify_error 分类、with_retry 快速失败、loop 错误事件带分类。"""
from __future__ import annotations

import pytest

from mini_cc.core.recovery import RecoveryState, classify_error, with_retry


class RateLimitError(Exception): pass
class OverloadedError(Exception): pass
class AuthError(Exception): pass


# ── classify_error ──────────────────────────────────────────────

@pytest.mark.parametrize("exc,kind,transient", [
    (RateLimitError("429 too many requests"), "rate_limit", True),
    (OverloadedError("529 overloaded"), "overloaded", True),
    (ConnectionError("connection reset by peer"), "network", True),
    (TimeoutError("request timed out"), "network", True),
    (RuntimeError("insufficient quota / billing hard limit"), "quota", False),
    (AuthError("invalid_api_key"), "auth", False),
    (AuthError("authentication error 401"), "auth", False),
    (RuntimeError("invalid_request_error: max_tokens > limit"), "invalid_request", False),
    (ValueError("something novel"), "unknown", False),
])
def test_classify_error(exc, kind, transient):
    cls = classify_error(exc)
    assert cls.kind == kind and cls.transient is transient


def test_rate_limit_wins_over_quota_wording():
    # "Rate limit ... quota" 必须按限流处理（与现状 429 优先一致）
    cls = classify_error(RateLimitError("Rate limit reached (TPM quota)"))
    assert cls.kind == "rate_limit" and cls.transient


# ── with_retry 快速失败 ────────────────────────────────────────

def test_with_retry_fail_fast_on_permanent(monkeypatch):
    monkeypatch.setattr("mini_cc.core.recovery.time.sleep",
                        lambda _: pytest.fail("must not sleep on permanent"))
    calls = []
    def fn():
        calls.append(1)
        raise AuthError("invalid_api_key")
    with pytest.raises(AuthError):
        with_retry(fn, RecoveryState())
    assert len(calls) == 1  # 未重试


def test_with_retry_retries_transient_network(monkeypatch):
    sleeps = []
    monkeypatch.setattr("mini_cc.core.recovery.time.sleep", sleeps.append)
    state = [0]
    def fn():
        state[0] += 1
        if state[0] < 3:
            raise ConnectionError("connection reset")
        return "ok"
    assert with_retry(fn, RecoveryState()) == "ok"
    assert len(sleeps) == 2


# ── loop 集成（fixture 模式照抄 tests/test_p0_loop_mocked.py）──

def _build_error_loop(tmp_path, exc):
    # 复用 test_p0_loop_mocked.py 的 _MockClient/_Block/_MockResponse/
    # _build_loop 骨架，但 client.create/stream 抛 exc 而非返回脚本。
    # 实现方式：把该文件的辅助类 import 进来或原样复制，然后：
    #   client 抛 exc → AgentLoop run 第一个事件流即出错
    ...


def test_loop_error_event_carries_classification(tmp_path):
    events = list(_build_error_loop(tmp_path, ConnectionError("connection reset")))
    err = [e for e in events if e["type"] == "error"]
    assert len(err) == 1
    assert err[0]["error_class"] == "network"
    assert err[0]["transient"] is True


def test_loop_transcript_marks_transient(tmp_path):
    loop = _build_error_loop(tmp_path, ConnectionError("connection reset"))
    list(loop.run("hi"))
    texts = [b.get("text", "") for m in loop.messages
             for b in m.get("content", []) if isinstance(b, dict)]
    assert any(t.startswith("[Error][transient]") for t in texts)
```

（`_build_error_loop` 按 test_p0_loop_mocked.py 的 `_MockClient` 写一个抛异常变体：`create`/`stream` 直接 `raise exc`；用 `_build_loop` 同款 ProjectRef 组装。执行者先读该文件再补全，断言不变。）

**Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_m4_error_classification.py -v`
Expected: FAIL（`ImportError: cannot import name 'classify_error'`）

**Step 3: recovery.py 实现**

在 `recovery.py` 顶部 dataclass 区新增；**分类顺序：rate_limit → overloaded 先于 quota/auth**（429 消息可能含 "quota" 字样，必须保持现行重试行为）：

```python
@dataclass(frozen=True)
class ErrorClass:
    kind: str      # rate_limit|overloaded|network|quota|auth|invalid_request|unknown
    transient: bool

_TRANSIENT_KINDS = frozenset({"rate_limit", "overloaded", "network"})


def classify_error(e: Exception) -> ErrorClass:
    """Classify an exception for retry policy + error-event typing.

    Ordering matters: rate_limit/overloaded are matched before the
    permanent kinds because provider rate-limit messages often contain
    words like "quota" — a 429 must stay retryable (pre-existing
    behavior).
    """
    name = type(e).__name__.lower()
    msg = str(e).lower()
    if "ratelimit" in name or "429" in msg:
        kind = "rate_limit"
    elif "overloaded" in name or "529" in msg or "overloaded" in msg:
        kind = "overloaded"
    elif ("quota" in msg or "credit" in msg or "balance" in msg
          or "402" in msg):
        kind = "quota"
    elif ("invalid_api_key" in msg or "authentication" in msg
          or "api key" in msg or "permission" in msg or "401" in msg
          or "403" in msg):
        kind = "auth"
    elif ("invalid_request" in msg or "not_found_error" in msg
          or "modelnotfound" in msg or "400" in msg):
        kind = "invalid_request"
    elif ("timeout" in name or "timed out" in msg or "connection" in msg
          or "eof" in msg or "reset" in msg):
        kind = "network"
    else:
        kind = "unknown"
    return ErrorClass(kind=kind, transient=kind in _TRANSIENT_KINDS)
```

改 `with_retry` 的 except 分支（保留 429/529 现有退避+fallback 逻辑，只是经由分类器进入；新增 network 通用退避重试）：

```python
        except Exception as e:
            cls = classify_error(e)
            if not cls.transient:
                # M4-2: quota/auth/invalid_request 类永久错误——重试无意义，
                # 快速失败并附分类事件，让上层与客户端能看到原因类别。
                if on_event:
                    on_event({"type": "retry_aborted", "reason": cls.kind,
                              "error": str(e)})
                raise
            if cls.kind == "rate_limit":
                d = retry_delay(attempt)
                if on_event:
                    on_event({"type": "retry", "reason": "429",
                              "attempt": attempt + 1, "delay": d})
                time.sleep(d)
                continue
            if cls.kind == "overloaded":
                state.consecutive_529 += 1
                cfg_fallback = cfg.fallback_model
                if (state.consecutive_529 >= 2 and cfg_fallback
                        and state.current_model != cfg_fallback):
                    state.current_model = cfg_fallback
                    state.consecutive_529 = 0
                    if on_event:
                        on_event({"type": "fallback_model",
                                  "model": cfg_fallback})
                d = retry_delay(attempt)
                if on_event:
                    on_event({"type": "retry", "reason": "529",
                              "attempt": attempt + 1, "delay": d})
                time.sleep(d)
                continue
            # network（瞬态）——通用退避
            d = retry_delay(attempt)
            if on_event:
                on_event({"type": "retry", "reason": cls.kind,
                          "attempt": attempt + 1, "delay": d})
            time.sleep(d)
            continue
```

**Step 4: loop.py except 路径（L803-837）**

`except Exception as e:` 块内，在 `self._record_request_status("error")` 之后加 `cls = classify_error(e)`；transcript 文本与事件改为：

```python
                prefix = "[Error][transient]" if cls.transient else "[Error]"
                self.messages.append({"role": "assistant", "content": [
                    {"type": "text",
                     "text": f"{prefix} {type(e).__name__}: {e}"}]})
                yield {"type": "error", "message": str(e),
                       "error_class": cls.kind, "transient": cls.transient}
```

（`is_prompt_too_long_error` 分支保持在其前不动；import 行 `from .recovery import ...` 追加 `classify_error`。）

**Step 5: 跑测试 → 全量**

Run: `python -m pytest tests/test_m4_error_classification.py -v` → PASS
Run: `python -m pytest tests/ -q` → 全绿（既有错误路径测试如断言 `[Error] ` 前缀需同步更新——全量红了就改测试断言为新前缀，属预期内）。

**Step 6: Commit**

```bash
git add mini_cc/core/recovery.py mini_cc/core/loop.py tests/test_m4_error_classification.py
git commit -m "feat(m4): error classifier — fail-fast retry + typed error events" # + 署名
```

---

### Task 3: M4-7 — 保守并行工具执行

**Files:**
- Modify: `mini_cc/tools/base.py`（FunctionTool 加字段）
- Modify: `mini_cc/tools/fs.py`（READ/GLOB/GREP 三工具置 True）、`mini_cc/tools/web.py`（WEB_FETCH）、`mini_cc/tools/websearch.py`（web_search）
- Modify: `mini_cc/core/loop.py`（`_execute_tool_calls` 两阶段化 + 新助手）
- Test: `tests/test_m4_parallel_tools.py`

**Step 1: 写失败测试**

```python
"""M4-7: parallel_safe 白名单 + 同轮 tool_use 并行执行。"""
from __future__ import annotations

import time

from mini_cc.core.loop import AgentLoop, ProjectRef
from mini_cc.sandbox import SubprocessSandbox
from mini_cc.storage import FSStorage
from mini_cc.tools.base import FunctionTool


def _slow_tool(name, seconds, parallel_safe=False, fn=None):
    return FunctionTool(
        name=name, description=f"slow {name}",
        input_schema={"type": "object", "properties": {}, "required": []},
        fn=fn or (lambda ctx, args: (time.sleep(seconds), f"{name} done")[1]),
        parallel_safe=parallel_safe)


def _loop_with(tmp_path, tools):
    sandbox = SubprocessSandbox("p", tmp_path / "ws")
    storage = FSStorage(tmp_path / "state")
    ref = ProjectRef(project_id="p", project_root=str(tmp_path / "ws"),
                     sandbox=sandbox, storage=storage,
                     client_factory=_echo_factory(tools))
    return AgentLoop(ref, "s", tools=tools)


# _echo_factory：照 test_p0_loop_mocked 的 _MockClient 骨架，脚本为：
#   第 1 响应：所有工具各一个 tool_use 块（stop_reason="tool_use"）
#   第 2 响应：单个 text 块 "done"（stop_reason="end_turn"）
# （执行者按 test_p0_loop_mocked.py 的 _Block/_MockResponse/_MockClient 复制改造）


def test_parallel_safe_flag(tmp_path):
    assert FunctionTool(
        name="x", description="", input_schema={}, fn=lambda c, a: "").parallel_safe is False
    from mini_cc.tools.fs import READ_TOOL, GLOB_TOOL, GREP_TOOL
    from mini_cc.tools.web import WEB_FETCH_TOOL
    assert READ_TOOL.parallel_safe and GLOB_TOOL.parallel_safe
    assert GREP_TOOL.parallel_safe and WEB_FETCH_TOOL.parallel_safe
    from mini_cc.tools.websearch import WEB_SEARCH_TOOL  # 名字以实际为准
    assert WEB_SEARCH_TOOL.parallel_safe


def test_parallel_batch_faster_than_serial(tmp_path):
    tools = [_slow_tool("a", 0.3, parallel_safe=True),
             _slow_tool("b", 0.3, parallel_safe=True)]
    loop = _loop_with(tmp_path, tools)
    t0 = time.monotonic()
    events = list(loop.run("go"))
    dt = time.monotonic() - t0
    ids = [e["id"] for e in events if e["type"] == "tool_use"]
    results = [e for e in events if e["type"] == "tool_result"]
    assert len(ids) == 2 and len(results) == 2
    assert dt < 0.55, f"两个 0.3s 并行工具应 <0.55s，实测 {dt:.2f}s"


def test_events_all_uses_before_results_in_batch(tmp_path):
    tools = [_slow_tool("a", 0.05, parallel_safe=True),
             _slow_tool("b", 0.05, parallel_safe=True)]
    events = list(_loop_with(tmp_path, tools).run("go"))
    seq = [(e["type"], e.get("id") or e.get("tool_use_id")) for e in events
           if e["type"] in ("tool_use", "tool_result")]
    types = [t for t, _ in seq]
    assert types.index("tool_result") > types.count("tool_use") - 1
    # 全部 tool_use 事件先于任何 tool_result（批内先发 use 再收结果）
    first_result = types.index("tool_result")
    assert all(t == "tool_use" for t in types[:first_result])


def test_workers_env_disables_parallel(tmp_path, monkeypatch):
    monkeypatch.setenv("MINI_CC_TOOL_WORKERS", "1")
    tools = [_slow_tool("a", 0.3, parallel_safe=True),
             _slow_tool("b", 0.3, parallel_safe=True)]
    t0 = time.monotonic()
    list(_loop_with(tmp_path, tools).run("go"))
    dt = time.monotonic() - t0
    assert dt >= 0.55, f"workers=1 应回退串行（≥0.6s），实测 {dt:.2f}s"


def test_unflagged_tool_stays_serial(tmp_path):
    tools = [_slow_tool("a", 0.3),  # 未置 parallel_safe
             _slow_tool("b", 0.3)]
    t0 = time.monotonic()
    list(_loop_with(tmp_path, tools).run("go"))
    assert time.monotonic() - t0 >= 0.55
```

**Step 2: 确认失败**

Run: `python -m pytest tests/test_m4_parallel_tools.py -v`
Expected: FAIL（`TypeError: unexpected keyword 'parallel_safe'`）

**Step 3: 实现**

1. `tools/base.py` FunctionTool 尾部加字段：
   ```python
   @dataclass
   class FunctionTool:
       name: str
       description: str
       input_schema: dict
       fn: Callable[[ToolContext, dict], str]
       # M4-7: 显式声明的只读工具可在同一轮 tool_use 批内并行执行。
       # 默认 False——保守并行，只有白名单工具才进线程池。
       parallel_safe: bool = False
   ```
   （Tool Protocol 不加该成员——结构性协议不强制实现者，loop 侧用 `getattr` 默认 False。）
2. fs.py 的 `READ_TOOL`/`GLOB_TOOL`/`GREP_TOOL`、web.py 的 `WEB_FETCH_TOOL`、websearch.py 的搜索工具构造处加 `parallel_safe=True`（websearch 的常量名以文件实际为准，`name="web_search"` 在其 :112 附近）。
3. loop.py：
   - 顶部加 `import os`（若已有则略）与并发导入放方法内（见下）。
   - 模块级新增：
     ```python
     def _tool_worker_count() -> int:
         """M4-7: 并行工具线程数。MINI_CC_TOOL_WORKERS 可调（默认 4，
         <2 视为 1 即纯串行）。"""
         try:
             n = int(os.environ.get("MINI_CC_TOOL_WORKERS", "4"))
         except ValueError:
             n = 4
         return max(1, min(n, 16))
     ```
   - 抽出单次执行助手（**Serial 路径行为逐字保留**，仅把 hooks/bg/handler 段收进来）：
     ```python
     def _run_one(self, ctx, name, tool_input, tool_use_id):
         """Execute one tool call: PreToolUse hook → background offload →
         handler → PostToolUse hook. Permission prompting and the
         subagent-event sink stay with the caller (serial semantics)."""
         denied = (self.hooks.trigger(Hooks.PreToolUse, name, tool_input)
                   if self.hooks is not None else None)
         if denied is not None:
             return str(denied)
         bg = self.project.background
         if (bg is not None and should_run_background(name, tool_input)
                 and name in self._handlers):
             bg_id = bg.start(ctx, self._handlers, name, tool_input, tool_use_id)
             return (f"[Background task {bg_id} started] "
                     "Result will arrive as a task_notification.")
         tool = self._handlers.get(name)
         if tool is None:
             return f"Unknown tool: {name}"
         output = tool.handle(ctx, tool_input)
         if self.hooks is not None:
             self.hooks.trigger(Hooks.PostToolUse, name, tool_input, output)
         return output
     ```
   - 并行批次执行器：
     ```python
     def _run_parallel_batch(self, ctx, batch, workers):
         """M4-7: run a batch of parallel_safe calls concurrently.

         Eligibility (caller-checked) guarantees none of these use the
         permission prompt, hooks, the subagent sink, or background
         offload — completion order is the only visible delta vs serial.
         All tool_use events were already yielded; tool_result events
         are yielded in completion order."""
         from concurrent.futures import ThreadPoolExecutor, as_completed
         ctx.on_subagent_event = None
         with ThreadPoolExecutor(max_workers=workers) as pool:
             futures = {
                 pool.submit(self._run_one, ctx, n, inp, tid): (n, tid)
                 for (n, inp, tid) in batch}
             for fut in as_completed(futures):
                 _name, tid = futures[fut]
                 output = fut.result()
                 self._rounds_since_todo += 1  # 白名单工具永不是 todo_write
                 yield {"type": "tool_result", "tool_use_id": tid,
                        "content": output}
                 self._emit({"type": "tool_result", "tool_use_id": tid,
                             "content": output})
     ```
   - 重写 `_execute_tool_calls` 主循环（docstring 保留原样并追加一句并行说明）：每个 tool_use 先判 eligible——
     ```python
     eligible = (workers >= 2
                 and self.hooks is None
                 and name not in self.project.prompt_tools
                 and getattr(self._handlers.get(name), "parallel_safe", False))
     ```
     eligible → 收入 `batch` 列表并 yield/emit tool_use 后 `continue`；非 eligible → 若 `batch` 非空先 `yield from self._run_parallel_batch(ctx, batch, workers)` 清空，再走**现有串行路径**（tool_use yield → subagent sink 绑定 → 权限提示原样 → `output = self._run_one(ctx, name, tool_input, tool_use_id)` → todo 计数 → drain subagent_events → tool_result yield → todo_write 特例）。循环结束后 `if batch: yield from self._run_parallel_batch(...)` 清尾。
     注意：串行路径中原来 `denined/hooks/bg/handler/PostToolUse` 的整段代码已被 `_run_one` 吸收，权限提示的 deny 分支（自己 yield tool_result + `continue`）原样保留在串行路径。

**Step 4: 跑测试 → 全量**

Run: `python -m pytest tests/test_m4_parallel_tools.py -v` → PASS
Run: `python -m pytest tests/ -q` → 全绿（重点看 test_p0_loop_mocked / 后台任务 / 权限提示相关测试不回归）。

**Step 5: Commit**

```bash
git add mini_cc/tools/base.py mini_cc/tools/fs.py mini_cc/tools/web.py mini_cc/tools/websearch.py mini_cc/core/loop.py tests/test_m4_parallel_tools.py
git commit -m "feat(m4): conservative parallel tool execution (parallel_safe)" # + 署名
```

---

### Task 4: M4-3 — /readyz

**Files:**
- Create: `mini_cc/server/health.py`
- Modify: `mini_cc/server/app.py`（/health 之后注册 /readyz）
- Test: `tests/test_m4_readyz.py`

**语义（已核实降级行为）**：docker 不可达时 runtime_context 优雅降级 subprocess（记录 DegradeEvent），故 docker 检查默认**非门控**（报告 degraded），`MINI_CC_READYZ_REQUIRE_DOCKER=1` 可改为门控。门控项 = storage 可写 + llm_configured。

**Step 1: 写失败测试**

```python
"""M4-3: /readyz 探针——storage 门控、docker 降级非门控、TTL 缓存。"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from mini_cc.sandbox.osdetect import DockerAvailability
from mini_cc.server import health as health_mod
from mini_cc.server.app import build_app


@pytest.fixture(autouse=True)
def _reset_caches(monkeypatch):
    health_mod._docker_state.update(ts=0.0, available=None, reason="",
                                    version="")
    yield


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr("mini_cc.config.default_config",
                        lambda: SimpleNamespace(
                            has_llm_credentials=lambda: True))
    app = build_app(data_dir=tmp_path)   # build_app 参数名以实际签名为准
    return TestClient(app)


def _fake_docker(monkeypatch, available, reason=""):
    def probe(*, force=False):
        return DockerAvailability(available=available, reason=reason)
    monkeypatch.setattr("mini_cc.sandbox.probe_docker", probe)


def test_readyz_200_when_storage_and_llm_ok(client, monkeypatch):
    _fake_docker(monkeypatch, available=False, reason="no docker")
    r = client.get("/readyz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ready"
    assert body["checks"]["storage"]["ok"] is True
    assert body["checks"]["llm_configured"]["ok"] is True
    assert body["checks"]["docker"]["ok"] is False   # 报告状态
    assert body["checks"]["docker"]["gating"] is False  # 但不门控


def test_readyz_503_when_llm_unconfigured(client, monkeypatch):
    monkeypatch.setattr("mini_cc.config.default_config",
                        lambda: SimpleNamespace(
                            has_llm_credentials=lambda: False))
    _fake_docker(monkeypatch, available=True)
    assert client.get("/readyz").status_code == 503


def test_readyz_docker_gating_opt_in(client, monkeypatch):
    monkeypatch.setenv("MINI_CC_READYZ_REQUIRE_DOCKER", "1")
    _fake_docker(monkeypatch, available=False, reason="daemon down")
    r = client.get("/readyz")
    assert r.status_code == 503
    assert r.json()["checks"]["docker"]["gating"] is True


def test_readyz_result_cached(client, monkeypatch):
    calls = []
    def probe(*, force=False):
        calls.append(force)
        return DockerAvailability(available=True)
    monkeypatch.setattr("mini_cc.sandbox.probe_docker", probe)
    client.get("/readyz"); client.get("/readyz")
    assert len(calls) == 1  # 5s TTL 内 docker 探针只跑一次
```

（`build_app(data_dir=...)` 的真实签名以 app.py 为准——构造时必须能落到 tmp_path；若签名不同（如 runtime_context 注入），按 app.py 现有测试的 app 构造方式改。）

**Step 2: 确认失败**

Run: `python -m pytest tests/test_m4_readyz.py -v`
Expected: FAIL（404 / 没有 /readyz）

**Step 3: 实现**

`mini_cc/server/health.py`：

```python
"""M4-3: readiness checks for /readyz — heavier probes than /health.

/health answers "process alive"; /readyz answers "can serve". Gating
checks: storage writable + LLM credentials. Docker is reported but
non-gating by default (the runtime degrades to the subprocess sandbox
when Docker is down — see runtime_context._sandbox_factory); set
MINI_CC_READYZ_REQUIRE_DOCKER=1 to make it gating.
"""
from __future__ import annotations

import os
import time

_DOCKER_TTL = 30.0
_READYZ_TTL = 5.0

_docker_state: dict = {"ts": 0.0, "available": None, "reason": "",
                       "version": ""}
_readyz_cache: dict = {"ts": 0.0, "payload": None}


def _probe_docker_cached() -> dict:
    from ..sandbox import probe_docker
    now = time.monotonic()
    if (_docker_state["available"] is None
            or now - _docker_state["ts"] > _DOCKER_TTL):
        d = probe_docker(force=True)
        _docker_state.update(ts=now, available=d.available,
                             reason=d.reason, version=d.server_version)
    return _docker_state


def _storage_check(data_dir) -> tuple[bool, str]:
    try:
        from pathlib import Path
        root = Path(data_dir)
        root.mkdir(parents=True, exist_ok=True)
        probe = root / ".readyz-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True, "writable"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def run_readiness_checks(data_dir) -> dict:
    from ..config import default_config
    storage_ok, storage_msg = _storage_check(data_dir)
    cfg = default_config()
    llm_ok = bool(getattr(cfg, "has_llm_credentials", lambda: False)())
    docker = _probe_docker_cached()
    gating = os.environ.get("MINI_CC_READYZ_REQUIRE_DOCKER", "") == "1"
    docker_ok = bool(docker["available"]) or not gating
    checks = {
        "storage": {"ok": storage_ok, "detail": storage_msg},
        "llm_configured": {"ok": llm_ok},
        "docker": {"ok": bool(docker["available"]), "gating": gating,
                   "detail": docker["reason"] or docker["version"]},
    }
    ready = storage_ok and llm_ok and docker_ok
    return {"status": "ready" if ready else "unready", "checks": checks}


def readiness_payload(data_dir) -> dict:
    """TTL-cached run_readiness_checks — /readyz may be scraped often."""
    now = time.monotonic()
    if _readyz_cache["payload"] is None or now - _readyz_cache["ts"] > _READYZ_TTL:
        _readyz_cache["payload"] = run_readiness_checks(data_dir)
        _readyz_cache["ts"] = now
    return _readyz_cache["payload"]
```

`app.py` 在 `/health` 定义后加：

```python
    @app.get("/readyz", tags=["meta"])
    def readyz() -> JSONResponse:
        """M4-3: readiness (storage/llm gating, docker informational).
        Cached 5s — probes touch the filesystem and may spawn docker."""
        from .health import readiness_payload
        payload = readiness_payload(pm.data_dir)
        return JSONResponse(
            status_code=200 if payload["status"] == "ready" else 503,
            content=payload)
```

（`pm` 为 build_app 里已有的 ProjectManager 实例，`pm.data_dir` 存在——manager.py:146；`JSONResponse` 已在 app.py 导入。测试里的 `_reset_caches` 同时应重置 `_readyz_cache`——在 fixture 里也 update 一下，避免跨测试污染。）

**Step 4: 跑测试 → 全量 → Commit**

Run: `python -m pytest tests/test_m4_readyz.py -v` → PASS；`python -m pytest tests/ -q` → 全绿。

```bash
git add mini_cc/server/health.py mini_cc/server/app.py tests/test_m4_readyz.py
git commit -m "feat(m4): /readyz readiness probes" # + 署名
```

---

### Task 5: M4-4 — 指标补全

**Files:**
- Modify: `mini_cc/server/metrics.py`（default_registry 注册新族）
- Modify: `mini_cc/core/loop.py`（`_run_one` 埋点）
- Modify: `mini_cc/mcp/client.py`（MCPPool 可选 metrics + `_record_attempt` 埋点）
- Modify: `mini_cc/teams/bus.py`（MessageBus 可选 metrics + `_try_file_lock` 埋点；Spawner 透传）
- Modify: `mini_cc/projects/manager.py:307,337`（构造点传 metrics）
- Test: `tests/test_m4_metrics_coverage.py`

**Step 1: 写失败测试**

```python
"""M4-4: tool/MCP/mailbox-lock 指标族与埋点。"""
from __future__ import annotations

import threading

from mini_cc.server.metrics import default_registry


def test_registry_has_new_families():
    reg = default_registry()
    assert "tool_calls_total" in reg.counters
    assert ("tool", "outcome") == reg.counters["tool_calls_total"].label_names
    assert "tool_duration_seconds" in reg.histograms
    assert "mcp_servers_total" in reg.counters
    assert "mailbox_lock_wait_seconds" in reg.histograms


def test_tool_metrics_recorded(tmp_path):
    # 照 test_m4_parallel_tools / test_p0_loop_mocked 的 fixture：
    # frozen tools = [正常 FunctionTool("ok"), 抛异常 FunctionTool("boom")]
    # reg = default_registry(); ProjectRef(..., metrics=reg)
    # script: 第 1 响应两个 tool_use，第 2 响应 text end_turn
    ...
    assert reg.counters["tool_calls_total"].value(tool="ok", outcome="success") == 1
    assert reg.counters["tool_calls_total"].value(tool="boom", outcome="failure") == 1
    assert reg.histograms["tool_duration_seconds"].items()


def test_mcp_connect_attempts_counted():
    from mini_cc.mcp import MCPPool
    reg = default_registry()
    pool = MCPPool("p1", metrics=reg)
    pool.register_factory("good", lambda: object())
    pool.connect("good")
    pool.connect("nope")
    assert reg.counters["mcp_servers_total"].value(status="connected") == 1
    assert reg.counters["mcp_servers_total"].value(status="failed") == 1
    MCPPool.reset_factories()


def test_mailbox_lock_wait_observed(tmp_path):
    from mini_cc.teams.bus import MessageBus
    reg = default_registry()
    bus = MessageBus(tmp_path, metrics=reg)
    bus.send("a", "b", "hello")
    items = reg.histograms["mailbox_lock_wait_seconds"].items()
    assert items and items[0][3] >= 1  # count >= 1（元组末位是 total）
```

（`test_tool_metrics_recorded` 的 fixture 组装同 Task 3；ProjectRef 传 `metrics=reg`。）

**Step 2: 确认失败**

Run: `python -m pytest tests/test_m4_metrics_coverage.py -v`
Expected: FAIL（族不存在 / 构造器不接受 metrics）

**Step 3: 实现**

1. `metrics.py` `default_registry()` 末尾追加：
   ```python
   reg.counter("tool_calls_total",
               "Tool invocations by tool and outcome.",
               ("tool", "outcome"))
   reg.histogram("tool_duration_seconds",
                 "Tool execution latency in seconds.", ("tool",))
   reg.counter("mcp_servers_total",
               "MCP server connect attempts by status.", ("status",))
   reg.histogram("mailbox_lock_wait_seconds",
                 "Teammate mailbox file-lock acquisition wait.",
                 (), buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1,
                              0.25, 0.5, 1.0, 2.5))
   ```
2. `loop.py`：`_run_one` 首尾埋点（Task 3 已建立的方法）——模块级助手：
   ```python
   def _is_error_output(output) -> bool:
       return isinstance(output, str) and output.startswith("Error:")

   # AgentLoop 内：
   def _record_tool_metrics(self, name, duration, outcome) -> None:
       reg = self.project.metrics
       if reg is None:
           return
       try:
           reg.counters["tool_calls_total"].inc(tool=name, outcome=outcome)
           reg.histograms["tool_duration_seconds"].observe(duration, tool=name)
       except Exception:
           pass  # 指标尽力而为
   ```
   `_run_one` 改为：
   ```python
   t0 = time.monotonic()
   try:
       output = <原有逻辑（可留原地或抽 _run_one_inner）>
   except Exception:
       self._record_tool_metrics(name, time.monotonic() - t0, "failure")
       raise
   self._record_tool_metrics(
       name, time.monotonic() - t0,
       "failure" if _is_error_output(output) else "success")
   return output
   ```
   （`time` 已在 loop.py 导入。）
3. `mcp/client.py`：`__init__(self, project_id: str, metrics=None)` 存 `self._metrics = metrics`；`_record_attempt` 末尾：
   ```python
   if self._metrics is not None:
       try:
           self._metrics.counters["mcp_servers_total"].inc(
               status="connected" if ok else "failed")
       except Exception:
           pass
   ```
   `manager.py:307` → `MCPPool(project_id, metrics=self.metrics)`（作用域内取 `_metrics`/`metrics` 以该类实际属性名为准——ProjectManager 存 `self.metrics`）。
4. `teams/bus.py`：`MessageBus.__init__(self, workspace: Path, metrics=None)` 存 `self._metrics`；`_try_file_lock` 进入时 `t0 = time.monotonic()`，成功取得锁后 observe `time.monotonic() - t0`（try/except 包裹，尽力而为）。`spawner.py` 的 `TeammateSpawner.__init__` 加 `metrics=None` 参数并 `self.bus = MessageBus(workspace, metrics=metrics)`；`manager.py:337` 构造点传 `metrics=...`。
5. `mini_cc/__init__.py` 与 `mini_cc/_shim.py` 的 `MCPPool("shim")` 等调用**不动**（metrics 默认 None）。

**Step 4: 跑测试 → 全量 → Commit**

Run: `python -m pytest tests/test_m4_metrics_coverage.py -v` → PASS；`python -m pytest tests/ -q` → 全绿。

```bash
git add mini_cc/server/metrics.py mini_cc/core/loop.py mini_cc/mcp/client.py mini_cc/teams/bus.py mini_cc/teams/spawner.py mini_cc/projects/manager.py tests/test_m4_metrics_coverage.py
git commit -m "feat(m4): tool/MCP/mailbox-lock metrics" # + 署名
```

---

### Task 6: 路线图收尾

**Files:**
- Modify: `docs/plans/2026-09-22-maintenance-roadmap.md`

**Step 1:** 勾选 M3-2（补实现注记：模块落位与行数变化、测试文件名）；M4 节勾选 M4-1/2/3/4/7 并在各项括号内补一句实现摘要；M4 标题状态改为 `🔶 部分完成（M4-5/6/8 待做）`。
**Step 2:** "完成记录" 表追加一行：`2026-09-24 | M3-2 + M4-1/2/3/4/7 | <各 commit hash> | 摘要`。
**Step 3:** Commit：

```bash
git add docs/plans/2026-09-22-maintenance-roadmap.md
git commit -m "docs(roadmap): record M3-2 + M4-1..4/7 completion" # + 署名
```

---

## 风险与回滚

- Task 1 循环导入：bus/protocol/convention 不得 import spawner 或彼此之外的新依赖；出现即调整 `__init__` 导入顺序。
- Task 3 是行为变更：并行工具在 CI 慢机上的时间断言（0.55s 阈值）留了 ~0.25s 余量；若仍 flaky，把阈值放宽到 0.58/收紧 sleep 到 0.35。
- Task 2 会改变错误事件 shape（新增字段）与 transcript 前缀——全量跑完后 grep 测试里对 `"[Error]"` 的断言同步更新。
- 每项独立 commit，出问题按项 revert。
