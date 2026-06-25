# mini_cc × OpenSandbox 三阶段集成实施计划

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 让 mini_cc 的容器沙箱后端可以从「本地 Docker (含 WSL2)」切换到「OpenSandbox lifecycle server」，对 agent 透明（`bash` / `read` / `write` 等工具无需感知），同时保留租户级隔离与现有 SubprocessSandbox 降级路径。

**Architecture:** 新增 `OpenSandboxRuntime` 作为 `ContainerRuntime` Protocol 的第三个实现（与 `DockerRuntime` / `FakeRuntime` 平级），通过 HTTP（stdlib `urllib`）调用远端 lifecycle server (`POST/GET/DELETE /sandboxes`) 与容器内 execd agent (`POST /command/run`，SSE 流)。租户↔sandbox 的幂等映射用 OpenSandbox 的 `metadata` 字段做指纹（`mini-cc-tid=<tid>`），不再依赖容器名。`ServerRuntimeContext` 通过 `MINI_CC_SANDBOX_BACKEND` env 在三套后端之间选择。Phase 2 抽象出 `MountSpec` 让 host/PVC/OSSFS 三类挂载统一表达；Phase 3 把 `code-execution REPL` 工具切到 OpenSandbox 的 code interpreter API，并预置一份可选项的 `.mcp.json` 模板给需要 ad-hoc 多镜像工作流的高级用户。

**Tech Stack:** Python 3.11 stdlib（`urllib.request` / `json` / `sseclient` 用一个 ~60 行的内联解析器；不引入 SDK 重依赖）。OpenSandbox lifecycle server v0.1+（spec: `specs/sandbox-lifecycle.yml` + `specs/execd-api.yaml`）。测试沿用现有 `monkeypatch subprocess.run` 风格，但在 `OpenSandboxRuntime` 上 monkeypatch `urllib.request.urlopen` 即可，无真实 server 依赖。

---

## Locked Decisions（锁定决策）

| 决策点 | 选择 | 理由 |
|---|---|---|
| **主集成路径** | HTTP Runtime adapter（不是 MCP） | mini_cc 的核心价值是租户隔离 + 工作区持久化，这两点在 MCP 模式下都依赖 LLM 自律（sandbox_id 字符串 + 进程内 ServerState）。HTTP adapter 把控制权留在服务端。 |
| **是否引入 opensandbox SDK** | 否，纯 stdlib HTTP | SDK 拖入 `mcp` / `pydantic` / async machinery；mini_cc 的 `mcp/http.py` 已经证明同等场景能用 urllib 干净完成。 |
| **同步 vs 异步** | 同步（阻塞） | `ContainerRuntime.exec()` 返回 `subprocess.CompletedProcess` 是同步契约；mini_cc 主流程是同步的；asyncio.run 在已运行 loop 的场景会炸。 |
| **Sandbox↔Tenant 幂等性** | metadata `mini-cc-tid=<tid>` | OpenSandbox sandbox id 是 server 发的 UUID，不能直接当 key。metadata 是 spec 原生支持的过滤维度 (`GET /sandboxes?metadata=...`)。注意 key 必须 DNS label 合法，故用连字符不用下划线。 |
| **execd 寻址** | `GET /sandboxes/{id}/endpoints/44772` 取 endpoint URL+headers | 与官方 SDK 一致（`adapters/command_adapter.py:144`）；Docker host 模式返回 localhost，K8s 模式返回 ingress gateway URL。 |
| **Mount 抽象时机** | Phase 2 才做 | Phase 1 先支持 host path bind mount（与现有 DockerRuntime 对齐），Phase 2 抽象 `MountSpec` 后再补 PVC/OSSFS。 |
| **降级策略** | 三级 fallback：opensandbox → docker → subprocess | 与现有 `ServerRuntimeContext` 自动降级语义一致；任何一层不可用就向下一层落。 |
| **MCP 路径** | Phase 3 预置可选 `.mcp.json` 模板 | 不拦着用户用 MCP，但不是默认路径。文档清楚说明权衡。 |
| **Commit 粒度** | 每个 Task 一个 commit | 沿用现有 git log 风格 (`feat(sandbox): ...`)。 |

## Supersedes / 与 P5 的关系

本计划**不替代** `2026-06-24-p5-container-sandbox-impl.md`（P5 已实现并上线）。P5 的 `DockerRuntime` / `TenantContainerManager` / `ContainerSandbox` 全部保留，本计划只**新增**一个并列后端 + 抽象层。Phase 2 对 `manager.py` 的 mount 表达做一次 refactor，向后兼容现有 `sandbox.toml` 格式。

## 生命周期对齐策略（OpenSandbox vs mini_cc）

mini_cc 把 sandbox 当**"租户长期工作区"**（一个 tenant 一个容器，所有 project 共享，活到 mini_cc 进程退出为止）。OpenSandbox 的设计更偏 **"任务级临时环境"**（spec 处处强调 timeout / TTL / auto-termination / snapshot）。这是根本性的语义错位，本计划采取以下策略对齐：

| 错位点 | 影响 | Phase 1 应对 | Phase 2+ 应对 |
|---|---|---|---|
| **TTL 强制超时** | mini_cc 长跑过 TTL 后 sandbox 消失，下次 ensure_running 自动重建（metadata 已无） | 仅支持 Docker runtime + `timeout: null`（manual cleanup mode）—— spec 明确只有部分 runtime provider 支持此模式 | K8s runtime 用 `renew-expiration` 接口定期续期；文档明确写明 TTL 上限受 server `max_sandbox_timeout_seconds` 约束 |
| **进程崩溃 → sandbox 泄漏** | mini_cc 异常退出时远端 sandbox 无 stop 信号，等 TTL 过期才清理 | 启动时调 `list_managed()` 清扫孤儿（state=Terminated/Failed 的 DELETE 掉）。dev 环境建议 `timeout=3600` 兜底 | 监控 + 告警：周期性对账 `list_managed` 与活跃 tenant 列表 |
| **Pending 异步 provisioning** | spec `POST` 返回 202 + state=Pending；mini_cc `ensure_running` 是同步契约 | Task 1.3 的 `_wait_running()` 轮询，默认 `ready_timeout=60s`。镜像未缓存时 cold-start 远超 60s——文档建议运维预先 `docker pull` | 可选：扩大 ready_timeout 到 300s，或让运维标记"已缓存镜像"白名单跳过轮询 |
| **execd 端点不稳定** | K8s ingress 模式下 signed URL 会过期 | 每次 `exec()` 都重新 GET endpoint（Task 1.5），多一次往返换简单性 | Docker host 模式下缓存 URL；缓存命中时跳过 discovery 调用 |

### 使用建议（写进运维文档）

- **适合的场景**：多租户共享集群、需要 K8s namespace 级强隔离、希望未来用 snapshot 做"租户工作区冻结/恢复"
- **不适合的场景**：单机 dev、tenant 长期活跃（>1 天无中断）、对每次 exec 延迟敏感（多一次 endpoint discovery HTTP）
- **推荐组合**：本地 dev 走 `MINI_CC_SANDBOX_BACKEND=docker`（P5 路径）；联调/staging 走 `opensandbox` 后端共享给团队；生产 K8s 形态 Phase 2 之后再启用

## File Map（三阶段总览）

```
mini_cc/sandbox/
  opensandbox_runtime.py    P1 NEW  ~280 行  OpenSandboxRuntime 实现
  osdetect.py               P1 MOD  +15 行   新增 probe_opensandbox()
  runtime_context.py        P1 MOD  +30 行   backend 选择逻辑
  cli.py (server)           P1 MOD  +20 行   sandbox status/stop 支持 opensandbox
  config.py                 P2 MOD  +40 行   MountSpec + sandbox.toml 扩展
  manager.py                P2 MOD  +60 行   tid metadata 索引 + MountSpec 消费
  runtime.py                P2 MOD  +20 行   DockerRuntime 适配 MountSpec
  container.py              P2 MOD  +5 行    无功能改动，仅类型hint
mini_cc/tools/
  repl.py                   P3 MOD  +50 行   backend 切换 (local|opensandbox)
  opensandbox_interp.py     P3 NEW  ~150 行  CodeInterpreter 适配器
mini_cc/sandbox/templates/
  opensandbox.mcp.json      P3 NEW  ~15 行   可选 MCP server 配置模板
docs/
  container-sandbox.md      P3 MOD  +80 行   OpenSandbox 后端使用文档
  opensandbox-integration.zh.md  P3 NEW  设计对照（本文档精简版）
tests/
  test_p6_os_runtime.py         P1 NEW  ~350 行
  test_p6_manager_metadata.py   P2 NEW  ~180 行
  test_p6_mount_spec.py         P2 NEW  ~140 行
  test_p6_code_interp.py        P3 NEW  ~120 行
.env.full.example               P1 MOD  +8 行  新增 OPENSANDBOX_* 变量
```

---

# Phase 1：OpenSandboxRuntime HTTP adapter（最小可用后端）

**Phase 1 目标：** 通过 `MINI_CC_SANDBOX_BACKEND=opensandbox` 启动 mini_cc 后，agent 调 `bash` / `execute` 工具时实际跑在远端 OpenSandbox sandbox 里，对 prompt 完全无感知。仅支持 Docker runtime + host bind mount 场景（K8s/PVC 留 Phase 2）。

**Phase 1 验收：**
- `test_p6_os_runtime.py` 全绿（~15 个测试，全部 mock HTTP）
- 手动跑：本地起 `opensandbox-server`，启动 mini_cc 设 `MINI_CC_SANDBOX_BACKEND=opensandbox`，让 agent 执行 `ls /workspaces`，能列出 tenant 项目目录内容
- `cmd_sandbox_status` / `cmd_sandbox_stop` 在 opensandbox 后端下正常工作

---

## Task 1.1：OpenSandboxConfig + env 加载

**Files:**
- Create: `mini_cc/sandbox/opensandbox_runtime.py`（本 task 只放 config 部分）
- Modify: `.env.full.example`
- Test: `tests/test_p6_os_runtime.py`（新建）

### Step 1：写失败测试

```python
# tests/test_p6_os_runtime.py
"""OpenSandboxRuntime: 通过 HTTP 调远端 lifecycle server + execd。
全部 monkeypatch urllib.request.urlopen，不依赖真实 server。"""
from __future__ import annotations

import os
from mini_cc.sandbox.opensandbox_runtime import OpenSandboxConfig


def test_config_from_env(monkeypatch):
    monkeypatch.setenv("OPEN_SANDBOX_API_KEY", "sk-test")
    monkeypatch.setenv("OPEN_SANDBOX_DOMAIN", "osb.example.com:8080")
    monkeypatch.setenv("OPEN_SANDBOX_PROTOCOL", "https")
    cfg = OpenSandboxConfig.from_env()
    assert cfg.base_url == "https://osb.example.com:8080/v1"
    assert cfg.api_key == "sk-test"
    assert cfg.execd_port == 44772


def test_config_defaults(monkeypatch):
    monkeypatch.delenv("OPEN_SANDBOX_API_KEY", raising=False)
    monkeypatch.delenv("OPEN_SANDBOX_DOMAIN", raising=False)
    cfg = OpenSandboxConfig.from_env()
    assert cfg.base_url == "http://172.28.76.178:11123/v1"
    assert cfg.api_key == ""  # 允许空（dev 模式）
```

### Step 2：跑测试看失败

```
pytest tests/test_p6_os_runtime.py -v
```
预期：`ImportError: cannot import name 'OpenSandboxConfig'`。

### Step 3：实现

```python
# mini_cc/sandbox/opensandbox_runtime.py
"""OpenSandboxRuntime: ContainerRuntime Protocol 的远端 HTTP 实现。

后端是 OpenSandbox lifecycle server (FastAPI :8080) + 容器内 execd (:44772)。
所有调用走 stdlib urllib，不依赖 opensandbox SDK。

幂等策略：每个 mini_cc 租户对应一个 OpenSandbox sandbox，通过 metadata 字段
`mini-cc-tid=<tid>` 索引（OpenSandbox spec 要求 metadata key 符合 DNS label
规则，故用连字符）。"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class OpenSandboxConfig:
    base_url: str          # e.g. "https://osb.example.com:8080/v1"
    api_key: str           # 可为空（dev 模式 server 不校验）
    execd_port: int = 44772
    request_timeout: int = 30
    poll_interval: float = 0.5
    ready_timeout: int = 60

    @classmethod
    def from_env(cls) -> "OpenSandboxConfig":
        proto = os.environ.get("OPEN_SANDBOX_PROTOCOL", "http")
        domain = os.environ.get("OPEN_SANDBOX_DOMAIN", "172.28.76.178:11123")
        return cls(
            base_url=f"{proto}://{domain}/v1",
            api_key=os.environ.get("OPEN_SANDBOX_API_KEY", ""),
        )
```

`.env.full.example` 追加：

```
# ── OpenSandbox backend (Phase P6) ─────────────────────────────────
# 当 MINI_CC_SANDBOX_BACKEND=opensandbox 时必填
# 默认指向开发机 172.28.76.178:11123（smoke 验证通过）
OPEN_SANDBOX_DOMAIN=172.28.76.178:11123
OPEN_SANDBOX_PROTOCOL=http
OPEN_SANDBOX_API_KEY=sk-opensandbox-123456
```

### Step 4：跑测试看通过

```
pytest tests/test_p6_os_runtime.py -v
```
预期：2 passed。

### Step 5：commit

```bash
git add mini_cc/sandbox/opensandbox_runtime.py tests/test_p6_os_runtime.py .env.full.example
git commit -m "feat(sandbox): OpenSandboxConfig + env loader"
```

---

## Task 1.2：HTTP 工具函数 + is_available

**Files:**
- Modify: `mini_cc/sandbox/opensandbox_runtime.py`
- Test: `tests/test_p6_os_runtime.py`

### Step 1：写失败测试

```python
# tests/test_p6_os_runtime.py 追加
import io, json
from urllib.request import Request
from mini_cc.sandbox.opensandbox_runtime import OpenSandboxRuntime


class _Resp(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *a): self.close()


def _mock_urlopen(monkeypatch, responder):
    """responder: callable(Request) -> (status, bytes, headers_dict)"""
    def fake(req: Request, *a, **kw):
        status, body, hdrs = responder(req)
        r = _Resp(body)
        r.status = status
        r.headers = {"Content-Type": "application/json", **(hdrs or {})}
        return r
    monkeypatch.setattr("mini_cc.sandbox.opensandbox_runtime.urlopen", fake)


def test_is_available_true(monkeypatch):
    cfg = OpenSandboxConfig(base_url="http://x/v1", api_key="k")
    _mock_urlopen(monkeypatch, lambda req: (200, b'{"status":"ok"}', {}))
    assert OpenSandboxRuntime(cfg).is_available() is True


def test_is_available_false_on_5xx(monkeypatch):
    cfg = OpenSandboxConfig(base_url="http://x/v1", api_key="k")
    _mock_urlopen(monkeypatch, lambda req: (503, b'{"error":"down"}', {}))
    assert OpenSandboxRuntime(cfg).is_available() is False


def test_is_available_false_on_conn_error(monkeypatch):
    cfg = OpenSandboxConfig(base_url="http://x/v1", api_key="k")
    def boom(req, *a, **kw):
        raise OSError("connection refused")
    monkeypatch.setattr("mini_cc.sandbox.opensandbox_runtime.urlopen", boom)
    assert OpenSandboxRuntime(cfg).is_available() is False
```

### Step 2：跑看失败

```
pytest tests/test_p6_os_runtime.py::test_is_available_true -v
```
预期：`AttributeError: 'OpenSandboxRuntime' object has no attribute 'is_available'`。

### Step 3：实现

```python
# opensandbox_runtime.py 追加
import json
import urllib.error
import urllib.request
from urllib.request import urlopen as _stdlib_urlopen  # 给测试 patch 用

# 重新绑定到模块级名字，方便 monkeypatch
urlopen = _stdlib_urlopen


def _request(cfg: OpenSandboxConfig, method: str, path: str,
             body: dict | None = None, timeout: int | None = None) -> tuple[int, dict]:
    url = f"{cfg.base_url}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if cfg.api_key:
        req.add_header("OPEN-SANDBOX-API-KEY", cfg.api_key)
    try:
        with urlopen(req, timeout=timeout or cfg.request_timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")
    except (urllib.error.URLError, OSError):
        return -1, {}


class OpenSandboxRuntime:
    def __init__(self, cfg: OpenSandboxConfig):
        self.cfg = cfg

    def is_available(self) -> bool:
        status, _ = _request(self.cfg, "GET", "/health", timeout=5)
        return status == 200
```

### Step 4：跑测试

```
pytest tests/test_p6_os_runtime.py -v
```
预期：5 passed。

### Step 5：commit

```bash
git add mini_cc/sandbox/opensandbox_runtime.py tests/test_p6_os_runtime.py
git commit -m "feat(sandbox): OpenSandboxRuntime._request + is_available"
```

---

## Task 1.3：ensure_running（核心幂等逻辑）

**Files:**
- Modify: `mini_cc/sandbox/opensandbox_runtime.py`
- Test: `tests/test_p6_os_runtime.py`

### Step 1：写失败测试

```python
def test_ensure_running_reuses_existing(monkeypatch):
    """已有同 tid 的 Running sandbox → 复用，不再 POST create。"""
    cfg = OpenSandboxConfig(base_url="http://x/v1", api_key="k", ready_timeout=5)
    calls = []
    def responder(req: Request):
        calls.append((req.method, req.full_url))
        if req.method == "GET" and "metadata=" in req.full_url:
            # 命中现有 sandbox
            return (200, json.dumps({"items":[
                {"id":"sbx_abc","status":{"state":"Running"},
                 "metadata":{"mini-cc-tid":"t1"}}], "pagination":{}}).encode(), {})
        if req.method == "POST":
            return (409, b'{"code":"CONFLICT"}', {})  # 不该走到
        return (404, b'{}', {})
    _mock_urlopen(monkeypatch, responder)
    rt = OpenSandboxRuntime(cfg)
    rt.ensure_running(name="t1", image="python:3.11",
                      mounts=[("/h","/workspaces","")], network="none")
    assert all("POST" not in c[0] for c in calls)


def test_ensure_running_creates_when_missing(monkeypatch):
    """没有命中 → POST create，轮询直到 Running。"""
    cfg = OpenSandboxConfig(base_url="http://x/v1", api_key="k",
                             ready_timeout=5, poll_interval=0.01)
    state_seq = ["Pending", "Running"]
    def responder(req: Request):
        m = req.method
        if m == "GET" and "metadata=" in req.full_url:
            return (200, b'{"items":[],"pagination":{}}', {})
        if m == "POST":
            return (202, json.dumps({"id":"sbx_new","status":{"state":"Pending"}}).encode(),
                    {"Location":"http://x/v1/sandboxes/sbx_new"})
        if m == "GET" and req.full_url.endswith("/sandboxes/sbx_new"):
            return (200, json.dumps({"id":"sbx_new",
                    "status":{"state": state_seq.pop(0)}}).encode(), {})
        return (404, b'{}', {})
    _mock_urlopen(monkeypatch, responder)
    rt = OpenSandboxRuntime(cfg)
    rt.ensure_running(name="t1", image="python:3.11",
                      mounts=[("/h","/workspaces","")], network="none")
    # 创建后轮询至少一次到 Running
    # （state_seq 被 pop 干净说明轮询发生过）


def test_ensure_running_timeout(monkeypatch):
    """ready_timeout 内一直 Pending → 抛 RuntimeError。"""
    cfg = OpenSandboxConfig(base_url="http://x/v1", api_key="k",
                             ready_timeout=1, poll_interval=0.05)
    def responder(req: Request):
        if req.method == "GET" and "metadata=" in req.full_url:
            return (200, b'{"items":[],"pagination":{}}', {})
        if req.method == "POST":
            return (202, b'{"id":"sbx_x","status":{"state":"Pending"}}', {})
        return (200, b'{"id":"sbx_x","status":{"state":"Pending"}}', {})
    _mock_urlopen(monkeypatch, responder)
    import pytest
    with pytest.raises(RuntimeError, match="never became Running"):
        OpenSandboxRuntime(cfg).ensure_running(
            name="t1", image="python:3.11",
            mounts=[("/h","/workspaces","")], network="none")
```

### Step 2：跑看失败

```
pytest tests/test_p6_os_runtime.py -k ensure_running -v
```
预期：`AttributeError: ... has no attribute 'ensure_running'`。

### Step 3：实现

```python
# opensandbox_runtime.py 追加
import time
from .runtime import RuntimeUnavailable


_TID_KEY = "mini-cc-tid"  # DNS-label 合法


def _find_by_tid(cfg: OpenSandboxConfig, tid: str) -> str | None:
    """list /sandboxes filtered by metadata → first matching id."""
    qs = f"?metadata={_TID_KEY}%3D{tid}&pageSize=1"
    status, body = _request(cfg, "GET", f"/sandboxes{qs}")
    if status != 200:
        return None
    items = body.get("items") or []
    return items[0]["id"] if items else None


def _wait_running(cfg: OpenSandboxConfig, sid: str) -> None:
    deadline = time.monotonic() + cfg.ready_timeout
    while time.monotonic() < deadline:
        status, body = _request(cfg, "GET", f"/sandboxes/{sid}")
        if status == 200:
            state = body.get("status", {}).get("state")
            if state == "Running":
                return
            if state in ("Failed", "Terminated"):
                raise RuntimeError(f"sandbox {sid} entered {state}")
        time.sleep(cfg.poll_interval)
    raise RuntimeError(f"sandbox {sid} never became Running")


class OpenSandboxRuntime:
    # ... (前一个 task 的方法保留)

    def ensure_running(self, *, name: str, image: str,
                       mounts: list[tuple[str, str, str]],
                       network: str, cpu_quota: str | None = None,
                       memory_limit: str | None = None) -> None:
        # name 在这里就是 tid（调用方 TenantContainerManager 传进来的）
        tid = name
        existing = _find_by_tid(self.cfg, tid)
        if existing is not None:
            return  # 已存在，复用
        body = {
            "image": {"uri": image},
            "entrypoint": ["tail", "-f", "/dev/null"],  # 长驻占位
            "metadata": {_TID_KEY: tid, "managed-by": "mini-cc"},
            "resourceLimits": {
                "cpu": cpu_quota or "1000m",
                "memory": memory_limit or "1Gi",
            },
            "timeout": 86400,  # 1 day; mini_cc 自己管 stop
            "volumes": [
                {"name": f"mnt-{i}", "host": {"path": h},
                 "mountPath": c}
                for i, (h, c, _opts) in enumerate(mounts)
            ],
        }
        if network in ("none", "disabled"):
            body["networkPolicy"] = {"defaultAction": "deny", "egress": []}
        status, resp = _request(self.cfg, "POST", "/sandboxes", body=body)
        if status not in (200, 202):
            raise RuntimeUnavailable(
                f"create sandbox failed: HTTP {status} {resp}")
        sid = resp.get("id")
        if not sid:
            raise RuntimeUnavailable(f"create returned no id: {resp}")
        _wait_running(self.cfg, sid)
```

### Step 4：跑测试

```
pytest tests/test_p6_os_runtime.py -k ensure_running -v
```
预期：3 passed。

### Step 5：commit

```bash
git add mini_cc/sandbox/opensandbox_runtime.py tests/test_p6_os_runtime.py
git commit -m "feat(sandbox): OpenSandboxRuntime.ensure_running with tid metadata dedup"
```

---

## Task 1.4：status + state mapping

**Files:**
- Modify: `mini_cc/sandbox/opensandbox_runtime.py`
- Test: `tests/test_p6_os_runtime.py`

### Step 1：写失败测试

```python
def test_status_running(monkeypatch):
    cfg = OpenSandboxConfig(base_url="http://x/v1", api_key="k")
    def responder(req):
        if "metadata=" in req.full_url:
            return (200, json.dumps({"items":[
                {"id":"sbx_a","status":{"state":"Running"}}]}).encode(), {})
        return (404, b'{}', {})
    _mock_urlopen(monkeypatch, responder)
    assert OpenSandboxRuntime(cfg).status("t1") == "running"


def test_status_terminated_maps_to_missing(monkeypatch):
    cfg = OpenSandboxConfig(base_url="http://x/v1", api_key="k")
    def responder(req):
        if "metadata=" in req.full_url:
            return (200, json.dumps({"items":[
                {"id":"sbx_a","status":{"state":"Terminated"}}]}).encode(), {})
        return (404, b'{}', {})
    _mock_urlopen(monkeypatch, responder)
    assert OpenSandboxRuntime(cfg).status("t1") == "missing"


def test_status_paused(monkeypatch):
    cfg = OpenSandboxConfig(base_url="http://x/v1", api_key="k")
    def responder(req):
        return (200, json.dumps({"items":[
            {"id":"sbx_a","status":{"state":"Paused"}}]}).encode(), {})
    _mock_urlopen(monkeypatch, responder)
    assert OpenSandboxRuntime(cfg).status("t1") == "paused"
```

### Step 2：跑看失败

```
pytest tests/test_p6_os_runtime.py::test_status_running -v
```

### Step 3：实现

```python
# OpenSandboxRuntime 类内
_STATE_MAP = {
    "Pending": "pending", "Running": "running", "Paused": "paused",
    "Pausing": "paused", "Resuming": "running",
    "Stopping": "exited", "Terminated": "missing", "Failed": "missing",
}

def status(self, name: str) -> str:
    sid = _find_by_tid(self.cfg, name)
    if sid is None:
        return "missing"
    status, body = _request(self.cfg, "GET", f"/sandboxes/{sid}")
    if status != 200:
        return "missing"
    state = body.get("status", {}).get("state", "Pending")
    return self._STATE_MAP.get(state, "missing")
```

### Step 4：跑测试 → 3 passed

### Step 5：commit

```bash
git commit -am "feat(sandbox): OpenSandboxRuntime.status with state mapping"
```

---

## Task 1.5：exec（核心命令执行）

**Files:**
- Modify: `mini_cc/sandbox/opensandbox_runtime.py`
- Test: `tests/test_p6_os_runtime.py`

### Step 1：写失败测试

```python
import subprocess

def test_exec_returns_completed_process(monkeypatch):
    cfg = OpenSandboxConfig(base_url="http://x/v1", api_key="k", execd_port=44772)
    def responder(req: Request):
        # 1) tid → sandbox_id
        if "metadata=" in req.full_url and req.method == "GET":
            return (200, json.dumps({"items":[
                {"id":"sbx_a","status":{"state":"Running"}}]}).encode(), {})
        # 2) execd endpoint discovery
        if "/endpoints/44772" in req.full_url:
            return (200, json.dumps({
                "endpoint":"osb-host/sandboxes/sbx_a/port/44772",
                "headers":{"X-Sandbox-Token":"tok"}}).encode(), {})
        return (404, b'{}', {})
    _mock_urlopen(monkeypatch, responder)

    # mock SSE 流（exec /command/run）
    sse_body = (
        b'event: init\ndata: {"execution_id":"e1"}\n\n'
        b'event: stdout\ndata: {"text":"hello\\n"}\n\n'
        b'event: stderr\ndata: {"text":"warn\\n"}\n\n'
        b'event: complete\ndata: {"exit_code":0}\n\n'
    )
    monkeypatch.setattr(
        "mini_cc.sandbox.opensandbox_runtime._stream_sse",
        lambda url, headers, body, timeout: sse_body)

    rt = OpenSandboxRuntime(cfg)
    cp = rt.exec(name="t1", workdir="/workspaces/p1",
                 command="echo hello", timeout=30, env={"FOO":"bar"})
    assert isinstance(cp, subprocess.CompletedProcess)
    assert cp.returncode == 0
    assert cp.stdout == "hello\n"
    assert cp.stderr == "warn\n"
```

### Step 2：跑看失败

### Step 3：实现

```python
# opensandbox_runtime.py 追加
def _stream_sse(url: str, headers: dict, body: bytes, timeout: int) -> bytes:
    """POST url with SSE response, return raw bytes.

    真实实现用 urllib.request + 手写 SSE 解析；这里默认走 stdlib。
    """
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    for k, v in headers.items():
        req.add_header(k, v)
    with urlopen(req, timeout=timeout) as r:
        return r.read()


def _parse_sse_output(raw: bytes) -> tuple[int, str, str]:
    """从 SSE 字节流拼出 (exit_code, stdout, stderr)。"""
    stdout, stderr = [], []
    exit_code = 0
    for raw_evt in raw.split(b"\n\n"):
        event_type = None
        data = b""
        for line in raw_evt.split(b"\n"):
            if line.startswith(b"event:"):
                event_type = line[6:].strip().decode()
            elif line.startswith(b"data:"):
                data = line[5:].strip()
        if not data:
            continue
        try:
            payload = json.loads(data)
        except json.JSONDecodeError:
            continue
        if event_type == "stdout":
            stdout.append(payload.get("text",""))
        elif event_type == "stderr":
            stderr.append(payload.get("text",""))
        elif event_type == "complete":
            exit_code = int(payload.get("exit_code", 0))
    return exit_code, "".join(stdout), "".join(stderr)


class OpenSandboxRuntime:
    # ... 之前的方法保留

    def _get_execd_endpoint(self, sid: str) -> tuple[str, dict]:
        status, body = _request(
            self.cfg, "GET", f"/sandboxes/{sid}/endpoints/{self.cfg.execd_port}")
        if status != 200:
            raise RuntimeUnavailable(
                f"cannot resolve execd endpoint for {sid}: HTTP {status}")
        # endpoint URL 在 spec 里是 host/path 形式，需要拼协议
        host = body["endpoint"]
        url = f"http://{host}/command/run" if "://" not in host else \
              f"{host}/command/run"
        return url, body.get("headers") or {}

    def exec(self, *, name: str, workdir: str, command: str,
             timeout: int, env: dict[str, str]) -> subprocess.CompletedProcess:
        sid = _find_by_tid(self.cfg, name)
        if sid is None:
            raise RuntimeUnavailable(
                f"no sandbox with tid={name}; call ensure_running first")
        url, hdrs = self._get_execd_endpoint(sid)
        payload = json.dumps({
            "command": command,
            "working_directory": workdir or None,
            "env": env or {},
        }).encode()
        raw = _stream_sse(url, hdrs, payload, timeout=timeout)
        rc, out, err = _parse_sse_output(raw)
        return subprocess.CompletedProcess(
            args=["opensandbox", "exec", name, command],
            returncode=rc, stdout=out, stderr=err)
```

### Step 4：跑测试 → 全部通过

### Step 5：commit

```bash
git commit -am "feat(sandbox): OpenSandboxRuntime.exec via execd SSE stream"
```

---

## Task 1.6：stop + remove + list_managed

**Files:**
- Modify: `mini_cc/sandbox/opensandbox_runtime.py`
- Test: `tests/test_p6_os_runtime.py`

### Step 1：写失败测试

```python
def test_stop_calls_delete(monkeypatch):
    cfg = OpenSandboxConfig(base_url="http://x/v1", api_key="k")
    deleted = []
    def responder(req):
        if "metadata=" in req.full_url and req.method == "GET":
            return (200, json.dumps({"items":[{"id":"sbx_a"}]}).encode(), {})
        if req.method == "DELETE":
            deleted.append(req.full_url)
            return (204, b'', {})
        return (404, b'{}', {})
    _mock_urlopen(monkeypatch, responder)
    OpenSandboxRuntime(cfg).stop("t1")
    assert any("/sandboxes/sbx_a" in u for u in deleted)


def test_remove_idempotent_when_missing(monkeypatch):
    cfg = OpenSandboxConfig(base_url="http://x/v1", api_key="k")
    def responder(req):
        if "metadata=" in req.full_url:
            return (200, b'{"items":[]}', {})
        return (404, b'{}', {})
    _mock_urlopen(monkeypatch, responder)
    OpenSandboxRuntime(cfg).remove("never-existed")  # 不抛


def test_list_managed_filters_metadata(monkeypatch):
    cfg = OpenSandboxConfig(base_url="http://x/v1", api_key="k")
    def responder(req):
        # 验证调用方附带了 managed-by=mini-cc 过滤
        assert "managed-by%3Dmini-cc" in req.full_url
        return (200, json.dumps({"items":[
            {"metadata":{"mini-cc-tid":"t1"}},
            {"metadata":{"mini-cc-tid":"t2"}},
        ]}).encode(), {})
    _mock_urlopen(monkeypatch, responder)
    names = OpenSandboxRuntime(cfg).list_managed()
    assert sorted(names) == ["t1", "t2"]
```

### Step 2：跑看失败

### Step 3：实现

```python
class OpenSandboxRuntime:
    # ...
    def stop(self, name: str) -> None:
        sid = _find_by_tid(self.cfg, name)
        if sid:
            _request(self.cfg, "DELETE", f"/sandboxes/{sid}")

    def remove(self, name: str) -> None:
        sid = _find_by_tid(self.cfg, name)
        if sid:
            _request(self.cfg, "DELETE", f"/sandboxes/{sid}")

    def list_managed(self, prefix: str = "mini_cc-") -> list[str]:
        # prefix 在 opensandbox 后端下被忽略；用 metadata 过滤
        qs = "?metadata=managed-by%3Dmini-cc&pageSize=200"
        status, body = _request(self.cfg, "GET", f"/sandboxes{qs}")
        if status != 200:
            return []
        return [it.get("metadata", {}).get(_TID_KEY, "")
                for it in body.get("items", [])
                if it.get("metadata", {}).get(_TID_KEY)]
```

### Step 4：跑测试 → 全绿

### Step 5：commit

```bash
git commit -am "feat(sandbox): OpenSandboxRuntime stop/remove/list_managed"
```

---

## Task 1.7：build_image 委托回 DockerRuntime

**Files:**
- Modify: `mini_cc/sandbox/opensandbox_runtime.py`
- Test: `tests/test_p6_os_runtime.py`

### Step 1：写失败测试

```python
def test_build_image_falls_back_to_docker(monkeypatch, tmp_path):
    """OpenSandbox 没有构建概念；委托给本地 DockerRuntime.build_image。"""
    from pathlib import Path
    captured = []
    monkeypatch.setattr(
        "mini_cc.sandbox.runtime.DockerRuntime.build_image",
        lambda self, tag, ctx, dockerfile=None: captured.append((tag, ctx)))
    cfg = OpenSandboxConfig(base_url="http://x/v1", api_key="k")
    OpenSandboxRuntime(cfg).build_image(
        "mini_cc-sandbox:latest", Path(tmp_path))
    assert captured == [("mini_cc-sandbox:latest", Path(tmp_path))]
```

### Step 2-4：实现 + 跑绿

```python
# OpenSandboxRuntime 类内
def build_image(self, tag: str, context_dir, dockerfile=None) -> None:
    """OpenSandbox 消费现成 image；本地仍需 build → 委托给 DockerRuntime。"""
    from .runtime import DockerRuntime
    DockerRuntime().build_image(tag, context_dir, dockerfile)
```

### Step 5：commit

```bash
git commit -am "feat(sandbox): OpenSandboxRuntime.build_image delegates to DockerRuntime"
```

---

## Task 1.8：ServerRuntimeContext 后端选择

**Files:**
- Modify: `mini_cc/server/runtime_context.py`
- Modify: `mini_cc/sandbox/osdetect.py`（新增 `probe_opensandbox`）
- Test: `tests/test_p6_os_runtime.py`（追加 backend 选择测试）

### Step 1：写失败测试

```python
def test_runtime_context_selects_opensandbox(monkeypatch):
    monkeypatch.setenv("MINI_CC_SANDBOX_BACKEND", "opensandbox")
    monkeypatch.setenv("OPEN_SANDBOX_DOMAIN", "osb:8080")
    monkeypatch.setenv("OPEN_SANDBOX_API_KEY", "sk")
    # mock is_available = True
    monkeypatch.setattr("mini_cc.sandbox.opensandbox_runtime.OpenSandboxRuntime.is_available",
                        lambda self: True)
    from mini_cc.server.runtime_context import ServerRuntimeContext
    ctx = ServerRuntimeContext.__new__(ServerRuntimeContext)
    # 直接调 _build_runtime（避免触发完整 __post_init__）
    rt = ServerRuntimeContext._build_runtime(ctx)
    from mini_cc.sandbox.opensandbox_runtime import OpenSandboxRuntime
    assert isinstance(rt, OpenSandboxRuntime)


def test_runtime_context_falls_back_when_opensandbox_unavailable(monkeypatch):
    monkeypatch.setenv("MINI_CC_SANDBOX_BACKEND", "opensandbox")
    monkeypatch.setattr("mini_cc.sandbox.opensandbox_runtime.OpenSandboxRuntime.is_available",
                        lambda self: False)
    monkeypatch.setattr("mini_cc.sandbox.osdetect.probe_docker",
                        lambda **kw: type("A",(),{"available":True,"argv_prefix":()})())
    from mini_cc.server.runtime_context import ServerRuntimeContext
    ctx = ServerRuntimeContext.__new__(ServerRuntimeContext)
    rt = ServerRuntimeContext._build_runtime(ctx)
    from mini_cc.sandbox.runtime import DockerRuntime
    assert isinstance(rt, DockerRuntime)
```

### Step 2-4：实现

```python
# mini_cc/server/runtime_context.py（重构现有 __post_init__ 中的 runtime 装配）
def _build_runtime(self):
    """根据 env 选后端：opensandbox → docker → None（降级 subprocess）。

    三级 fallback：任何一层不可用就向下一层落。"""
    import os
    from mini_cc.sandbox.runtime import DockerRuntime
    backend = os.environ.get("MINI_CC_SANDBOX_BACKEND", "auto")

    if backend in ("opensandbox", "auto"):
        try:
            from mini_cc.sandbox.opensandbox_runtime import (
                OpenSandboxConfig, OpenSandboxRuntime)
            rt = OpenSandboxRuntime(OpenSandboxConfig.from_env())
            if rt.is_available():
                return rt
        except Exception:
            pass
        if backend == "opensandbox":
            # 显式要 opensandbox 但不可用 → 继续往下试，记 warning
            import logging
            logging.getLogger("mini_cc").warning(
                "opensandbox backend requested but unavailable; falling back")

    if backend in ("docker", "auto"):
        from ..sandbox import probe_docker
        avail = probe_docker()
        if avail.available:
            return DockerRuntime(prefix=avail.argv_prefix)

    return None  # 调用方降级到 SubprocessSandbox
```

### Step 5：commit

```bash
git commit -am "feat(sandbox): ServerRuntimeContext three-tier backend selection"
```

---

## Task 1.9：CLI sandbox subcommand 适配

**Files:**
- Modify: `mini_cc/server/cli.py`

### Step 1-4：让 `cmd_sandbox_status` / `cmd_sandbox_stop` 在 opensandbox 后端下走 `OpenSandboxRuntime`

```python
# mini_cc/server/cli.py 修改 cmd_sandbox_status / stop / build_image
def _get_runtime():
    """根据 env 选 runtime 实例。"""
    import os
    backend = os.environ.get("MINI_CC_SANDBOX_BACKEND", "auto")
    if backend == "opensandbox":
        from ..sandbox.opensandbox_runtime import (
            OpenSandboxConfig, OpenSandboxRuntime)
        return OpenSandboxRuntime(OpenSandboxConfig.from_env())
    from ..sandbox.osdetect import probe_docker
    from ..sandbox.runtime import DockerRuntime
    return DockerRuntime(prefix=probe_docker(force=True).argv_prefix)
```

把三个 cmd_sandbox_* 函数里的 `DockerRuntime(prefix=...)` 替换为 `_get_runtime()`。`cmd_sandbox_build_image` 不变（OpenSandboxRuntime.build_image 已委托回 DockerRuntime）。

### Step 5：commit

```bash
git commit -am "feat(sandbox): CLI sandbox commands support opensandbox backend"
```

---

## Task 1.10：手动验收

无代码改动，纯验证步骤。

1. **起 OpenSandbox server**（另一终端）：
   ```bash
   uvx opensandbox-server init-config ~/.sandbox.toml --example docker
   uvx opensandbox-server
   ```
2. **配置 mini_cc**：
   ```
   export MINI_CC_SANDBOX_BACKEND=opensandbox
   export OPEN_SANDBOX_DOMAIN=localhost:8080
   export OPEN_SANDBOX_API_KEY=<填 server.toml 里的 api_key>
   ```
3. **启动 mini_cc**，看启动日志：
   ```
   starting server ... docker_available=True  ← 应改为 backend=opensandbox
   ```
4. **让 agent 跑 `ls /workspaces`**，预期：
   - 第一次调用触发 `POST /sandboxes`（看 server 日志）
   - 后续调用命中 metadata 索引，无新建
   - agent 输出 tenant 项目目录列表
5. **跑 `python -m mini_cc.server sandbox status`**：列出 `t1 running` 等
6. **跑 `python -m mini_cc.server sandbox stop <tid>`**：sandbox 被删除

记录任何失败场景到 followup。

### Phase 1 完成 commit（可选里程碑 tag）

```bash
git tag p6-phase1-done
```

---

# Phase 2：Manager 抽象 + metadata 索引重构

**Phase 2 目标：** 把 mount 表达从 `tuple(host, container, options)` 抽象成 `MountSpec` 数据类，为 K8s 后端的 PVC/OSSFS 留口子；同时把 `TenantContainerManager` 从「靠容器名做幂等」完全切到「靠 metadata 索引」，这样 `DockerRuntime` 也能受益（容器名只是给运维看的，不再决定路由）。

**Phase 2 验收：**
- `test_p6_mount_spec.py` 全绿（~8 测试）
- `test_p6_manager_metadata.py` 全绿（~10 测试）
- 现有 P5 测试（`test_p5_*`）保持全绿（向后兼容）
- 手动验证：K8s 模式下 tenant 用 PVC，挂载到 `/workspaces` 正常工作

---

## Task 2.1：MountSpec dataclass

**Files:**
- Modify: `mini_cc/sandbox/config.py`
- Test: `tests/test_p6_mount_spec.py`（新建）

### Step 1：写失败测试

```python
# tests/test_p6_mount_spec.py
from mini_cc.sandbox.config import MountSpec, HostMount, PVCMount


def test_host_mount_round_trip():
    m = MountSpec(name="w", mount_path="/workspaces",
                  backend=HostMount(path="/host/p"))
    assert m.backend.path == "/host/p"
    assert m.read_only is False


def test_pvc_mount_defaults():
    m = MountSpec(name="w", mount_path="/workspaces",
                  backend=PVCMount(claim_name="t1-pvc"))
    assert m.backend.create_if_not_exists is True
    assert m.backend.storage_class is None


def test_legacy_tuple_compat():
    """旧 tuple(host, container, options) 能转 MountSpec。"""
    m = MountSpec.from_legacy_tuple(("/h", "/workspaces", ""))
    assert m.name.startswith("mnt")
    assert m.mount_path == "/workspaces"
    assert isinstance(m.backend, HostMount)
```

### Step 2-4：实现

```python
# mini_cc/sandbox/config.py 追加
from dataclasses import dataclass, field
from typing import Literal, Union


@dataclass(frozen=True)
class HostMount:
    path: str


@dataclass(frozen=True)
class PVCMount:
    claim_name: str
    create_if_not_exists: bool = True
    storage_class: str | None = None
    storage: str | None = None


@dataclass(frozen=True)
class OSSFSMount:  # 占位；Phase 2 不一定实现，留接口
    bucket: str
    endpoint: str
    access_key_id: str
    access_key_secret: str


MountBackend = Union[HostMount, PVCMount, OSSFSMount]


@dataclass(frozen=True)
class MountSpec:
    name: str
    mount_path: str
    backend: MountBackend
    read_only: bool = False
    sub_path: str | None = None

    @classmethod
    def from_legacy_tuple(cls, t: tuple[str, str, str]) -> "MountSpec":
        host, container, _opts = t
        return cls(
            name="mnt-" + host.replace("/", "_").replace("\\", "_")[:40]
                       .strip("_") or "mnt",
            mount_path=container,
            backend=HostMount(path=host),
        )
```

### Step 5：commit

```bash
git commit -am "feat(sandbox): MountSpec abstraction (host/PVC/OSSFS)"
```

---

## Task 2.2：DockerRuntime 消费 MountSpec

**Files:**
- Modify: `mini_cc/sandbox/manager.py`
- Modify: `mini_cc/sandbox/runtime.py`
- Test: `tests/test_p6_mount_spec.py`

### Step 1：写失败测试

```python
def test_docker_runtime_accepts_mount_spec(monkeypatch):
    """DockerRuntime.ensure_running 同时支持 tuple 和 MountSpec。"""
    import subprocess
    captured = []
    monkeypatch.setattr(subprocess, "run",
        lambda a, **kw: captured.append(a) or subprocess.CompletedProcess(
            args=a, returncode=0, stdout="missing\n"))
    monkeypatch.setattr(subprocess, "run",
        lambda a, **kw: captured.append(a) or subprocess.CompletedProcess(
            args=a, returncode=0, stdout=""))
    # ↑ 第一次 inspect 返回 missing，第二次 run 创建
    from mini_cc.sandbox.runtime import DockerRuntime
    from mini_cc.sandbox.config import MountSpec, HostMount
    rt = DockerRuntime()
    rt.ensure_running(
        name="t1", image="img",
        mounts=[MountSpec(name="w", mount_path="/workspaces",
                          backend=HostMount(path="/host"))],
        network="none")
    run_calls = [c for c in captured if c[:2] == ["docker","run"]]
    assert any("/host:/workspaces" in a for a in run_calls[0])
```

### Step 2-4：实现

`runtime.py` 的 `DockerRuntime.ensure_running` 在循环里加类型分支：

```python
for mount in mounts:
    if isinstance(mount, MountSpec):
        if not isinstance(mount.backend, HostMount):
            raise ValueError(f"DockerRuntime only supports HostMount, got {type(mount.backend)}")
        host_arg = to_wsl_path(mount.backend.path) if self._prefix else mount.backend.path
        spec = f"{host_arg}:{mount.mount_path}"
        if mount.read_only:
            spec += ":ro"
    else:  # 兼容老 tuple
        host, container, options = mount
        host_arg = to_wsl_path(host) if self._prefix else host
        spec = f"{host_arg}:{container}"
        if options:
            spec += f":{options}"
    argv += ["-v", spec]
```

`manager.py` 的 `_mounts()` 返回 `list[MountSpec]`（仍允许 tuple 临时存在）。

### Step 5：commit

```bash
git commit -am "refactor(sandbox): DockerRuntime consumes MountSpec (tuple backwards-compat)"
```

---

## Task 2.3：OpenSandboxRuntime 消费 MountSpec（含 PVC）

**Files:**
- Modify: `mini_cc/sandbox/opensandbox_runtime.py`
- Test: `tests/test_p6_mount_spec.py`

### Step 1：写失败测试

```python
def test_opensandbox_translates_pvc_mount():
    """PVC mount → OpenSandbox volume.pvc block。"""
    from mini_cc.sandbox.config import MountSpec, PVCMount
    from mini_cc.sandbox.opensandbox_runtime import _mount_spec_to_volume
    m = MountSpec(name="w", mount_path="/workspaces",
                  backend=PVCMount(claim_name="t1-pvc", storage="5Gi"))
    vol = _mount_spec_to_volume(m)
    assert vol == {
        "name": "w", "mountPath": "/workspaces",
        "pvc": {"claimName": "t1-pvc", "storage": "5Gi",
                "createIfNotExists": True},
    }


def test_opensandbox_translates_host_mount():
    from mini_cc.sandbox.config import MountSpec, HostMount
    from mini_cc.sandbox.opensandbox_runtime import _mount_spec_to_volume
    m = MountSpec(name="w", mount_path="/workspaces",
                  backend=HostMount(path="/host"))
    vol = _mount_spec_to_volume(m)
    assert vol["host"] == {"path": "/host"}
    assert vol["mountPath"] == "/workspaces"
```

### Step 2-4：实现

```python
# opensandbox_runtime.py 追加
def _mount_spec_to_volume(m):
    v = {"name": m.name, "mountPath": m.mount_path}
    if isinstance(m.backend, HostMount):
        v["host"] = {"path": m.backend.path}
    elif isinstance(m.backend, PVCMount):
        v["pvc"] = {
            "claimName": m.backend.claim_name,
            "createIfNotExists": m.backend.create_if_not_exists,
        }
        if m.backend.storage_class:
            v["pvc"]["storageClass"] = m.backend.storage_class
        if m.backend.storage:
            v["pvc"]["storage"] = m.backend.storage
    elif isinstance(m.backend, OSSFSMount):
        v["ossfs"] = {
            "bucket": m.backend.bucket, "endpoint": m.backend.endpoint,
            "accessKeyId": m.backend.access_key_id,
            "accessKeySecret": m.backend.access_key_secret,
        }
    if m.read_only:
        v["readOnly"] = True
    if m.sub_path:
        v["subPath"] = m.sub_path
    return v
```

`ensure_running` 里改用 `_mount_spec_to_volume`：

```python
"volumes": [
    _mount_spec_to_volume(m if isinstance(m, MountSpec)
                           else MountSpec.from_legacy_tuple(m))
    for m in mounts
],
```

### Step 5：commit

```bash
git commit -am "feat(sandbox): OpenSandboxRuntime translates MountSpec (host+PVC)"
```

---

## Task 2.4：TenantContainerManager 切到 metadata 索引

**Files:**
- Modify: `mini_cc/sandbox/manager.py`
- Test: `tests/test_p6_manager_metadata.py`（新建）

### Step 1：写失败测试

```python
# tests/test_p6_manager_metadata.py
def test_manager_passes_tid_as_name_to_runtime():
    """ensure_running 把 tid（不是容器名）传给 runtime.name。"""
    from mini_cc.sandbox.manager import TenantContainerManager
    from mini_cc.sandbox.runtime import FakeRuntime
    from mini_cc.sandbox.config import ContainerConfig
    from pathlib import Path
    rt = FakeRuntime(available=True)
    cfg = ContainerConfig(image_tag="img")
    m = TenantContainerManager("my-tid", Path("/h"), cfg, rt)
    m.ensure_running()
    method, kwargs = rt.calls[0]
    assert kwargs["name"] == "my-tid"  # tid，不是 mini_cc-my-tid
```

### Step 2-4：实现

`manager.py`：

```python
def ensure_running(self) -> None:
    self.runtime.ensure_running(
        name=self.tid,  # ← 改：传 tid，让 runtime 自己决定怎么用它
        image=self.config.image_tag,
        mounts=self._mounts(),
        network=self.config.network,
        cpu_quota=self.config.cpu_quota,
        memory_limit=self.config.memory_limit)

# container_name 属性保留，仅用于人类可读的日志/CLI 显示
```

`DockerRuntime` 内部：把传进来的 `name`（现在是 tid）当作「逻辑标识」，仍要构造合法的 docker 容器名（`mini_cc-<sanitized_tid>`）：

```python
def ensure_running(self, *, name, ...):
    docker_name = _docker_name_from_tid(name)  # = "mini_cc-<sanitized>"
    # 后续都用 docker_name
```

抽 `_docker_name_from_tid` 出来。

### Step 5：commit

```bash
git commit -am "refactor(sandbox): TenantContainerManager passes tid; DockerRuntime synthesizes name"
```

---

## Task 2.5：P5 回归测试

跑 `pytest tests/test_p5_*.py -v`，确认所有 P5 测试仍绿。如果有挂的（因为 `name` 语义变了），fix 测试断言而不是改回 manager。

### Step 6：commit

```bash
git commit -am "test(sandbox): align P5 tests with tid-as-name contract"
```

---

# Phase 3：Code interpreter + 可选 MCP 预置

**Phase 3 目标：**
1. `code-execution REPL` 工具支持远端 backend：调用 OpenSandbox 的 code interpreter API（比 `exec sh -c` 更适合 stateful 代码执行，自带 context 复用）
2. 给项目模板预置一份可选 `.mcp.json`，让高级用户能直接通过 MCP 工具 ad-hoc 创建 sandbox

**Phase 3 验收：**
- `test_p6_code_interp.py` 全绿（~6 测试）
- 文档 `docs/container-sandbox.md` 增加 OpenSandbox 后端章节
- 手动验证：`/repl python` 工具在 opensandbox 后端下，连续两次执行 `x = 1`、`print(x)` 能输出 1（context 复用）

---

## Task 3.1：CodeInterpreter adapter 接口

**Files:**
- Create: `mini_cc/tools/opensandbox_interp.py`
- Test: `tests/test_p6_code_interp.py`（新建）

### Step 1：写失败测试

```python
# tests/test_p6_code_interp.py
def test_interp_runs_python_and_returns_value(monkeypatch):
    """OpenSandbox code interpreter: POST /code，返回 result + stdout。"""
    from mini_cc.tools.opensandbox_interp import OpenSandboxInterpreter
    interp = OpenSandboxInterpreter(
        sandbox_id="sbx_a",
        execd_url="http://osb/port/44772",
        execd_headers={"X-Sandbox-Token":"t"})

    monkeypatch.setattr(
        "mini_cc.tools.opensandbox_interp._post_code",
        lambda url, hdrs, body: {
            "result": [{"text":"4"}],
            "logs": {"stdout":[{"text":"3.11.14\n"}]},
        })
    out = interp.run("2 + 2", language="python")
    assert "4" in out
    assert "3.11.14" in out


def test_interp_creates_context_lazily(monkeypatch):
    """第一次调用时 POST /code/context 拿 context_id；后续复用。"""
    calls = []
    def fake_post(url, hdrs, body):
        calls.append(url)
        if url.endswith("/code/context"):
            return {"id":"ctx_1","language":"python"}
        return {"result":[],"logs":{"stdout":[{"text":"ok"}]}}
    monkeypatch.setattr("mini_cc.tools.opensandbox_interp._post_code", fake_post)
    interp = OpenSandboxInterpreter("sbx", "http://x", {})
    interp.run("x=1", language="python")
    interp.run("x", language="python")
    assert sum(1 for u in calls if u.endswith("/code/context")) == 1
```

### Step 2-4：实现

```python
# mini_cc/tools/opensandbox_interp.py
"""OpenSandbox code interpreter adapter.

走 execd 的 /code/context + /code 端点（spec: execd-api.yaml）。比
`docker exec sh -c "python -c ..."` 更适合 stateful：context_id 复用，
变量跨调用持久。"""
from __future__ import annotations

import json
import urllib.request


def _post_code(url: str, headers: dict, body: dict) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    for k, v in headers.items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


class OpenSandboxInterpreter:
    def __init__(self, sandbox_id: str, execd_url: str, execd_headers: dict):
        self.sandbox_id = sandbox_id
        self.execd_url = execd_url.rstrip("/")
        self.execd_headers = execd_headers
        self._contexts: dict[str, str] = {}  # language -> context_id

    def _ensure_context(self, language: str) -> str:
        if language in self._contexts:
            return self._contexts[language]
        resp = _post_code(f"{self.execd_url}/code/context",
                          self.execd_headers, {"language": language})
        cid = resp["id"]
        self._contexts[language] = cid
        return cid

    def run(self, code: str, *, language: str = "python") -> str:
        cid = self._ensure_context(language)
        resp = _post_code(
            f"{self.execd_url}/code", self.execd_headers,
            {"context": {"id": cid, "language": language}, "code": code})
        parts = []
        for entry in resp.get("result", []):
            if "text" in entry:
                parts.append(entry["text"])
        for stream in ("stdout", "stderr"):
            for entry in resp.get("logs", {}).get(stream, []):
                if entry.get("text"):
                    parts.append(entry["text"])
        return "\n".join(parts)
```

### Step 5：commit

```bash
git commit -am "feat(tools): OpenSandboxInterpreter adapter (stateful code execution)"
```

---

## Task 3.2：REPL 工具 backend 切换

**Files:**
- Modify: `mini_cc/tools/repl.py`
- Test: `tests/test_p6_code_interp.py`

### Step 1：写失败测试

```python
def test_repl_uses_opensandbox_when_configured(monkeypatch):
    """env MINI_CC_REPL_BACKEND=opensandbox 时，/repl 走 OpenSandboxInterpreter。"""
    monkeypatch.setenv("MINI_CC_REPL_BACKEND", "opensandbox")
    monkeypatch.setattr("mini_cc.tools.opensandbox_interp.OpenSandboxInterpreter.run",
                        lambda self, code, language="python": f"[osb:{code}]")
    # 通过 ToolContext 调
    from mini_cc.tools.repl import _run_repl
    ctx = type("C", (), {"sandbox": None, "runtime": None})()  # 简化
    out = _run_repl(ctx, {"language": "python", "code": "1+1"})
    assert "[osb:1+1]" in out
```

### Step 2-4：实现

在 `repl.py` 的 `_run_repl` 入口加 backend 分支：

```python
def _run_repl(ctx, args) -> str:
    import os
    backend = os.environ.get("MINI_CC_REPL_BACKEND", "local")
    if backend == "opensandbox":
        return _run_repl_opensandbox(ctx, args)
    return _run_repl_local(ctx, args)  # 现有逻辑重命名


def _run_repl_opensandbox(ctx, args) -> str:
    """通过 OpenSandbox code interpreter 执行；sandbox 由 sandbox 层提供。

    需要 ctx.runtime 是 OpenSandboxRuntime 且已 ensure_running。
    依赖 ctx 上有 tid → 反查 sandbox_id → 解析 execd endpoint。"""
    from mini_cc.tools.opensandbox_interp import OpenSandboxInterpreter
    rt = ctx.runtime
    if rt is None or not hasattr(rt, "_get_execd_endpoint"):
        return "Error: opensandbox REPL requires OpenSandboxRuntime"
    sid = rt._find_by_tid(rt.cfg, ctx.tid)
    if sid is None:
        return "Error: no sandbox; ensure_running must run first"
    url, hdrs = rt._get_execd_endpoint(sid)
    interp = OpenSandboxInterpreter(sid, url, hdrs)
    return interp.run(args["code"], language=args.get("language","python"))
```

### Step 5：commit

```bash
git commit -am "feat(tools): /repl switches backend via MINI_CC_REPL_BACKEND"
```

---

## Task 3.3：可选 MCP 模板 + 文档

**Files:**
- Create: `mini_cc/sandbox/templates/opensandbox.mcp.json`
- Modify: `docs/container-sandbox.md`

### Step 1：写模板

```json
{
  "mcpServers": {
    "opensandbox": {
      "type": "stdio",
      "command": "opensandbox-mcp",
      "args": [
        "--api-key", "${OPEN_SANDBOX_API_KEY}",
        "--domain", "${OPEN_SANDBOX_DOMAIN}",
        "--protocol", "${OPEN_SANDBOX_PROTOCOL:-http}"
      ],
      "env": {
        "OPEN_SANDBOX_API_KEY": "${OPEN_SANDBOX_API_KEY}",
        "OPEN_SANDBOX_DOMAIN": "${OPEN_SANDBOX_DOMAIN}"
      }
    }
  }
}
```

### Step 2：文档更新

`docs/container-sandbox.md` 追加章节《OpenSandbox 远端后端》：
- 何时用（多租户共享集群、跨机分布式、想用 K8s 资源管理）
- 何时**不**用 MCP 路径（需要租户强隔离、需要持久工作区——主推 HTTP adapter）
- env 配置（`MINI_CC_SANDBOX_BACKEND`、`OPEN_SANDBOX_*`）
- MountSpec 三类 backend 的取舍表
- Code interpreter REPL 的启用
- 可选 MCP 路径的预置方法 + 权衡说明（sandbox_id 字符串状态、租户隔离弱化）

### Step 3：commit

```bash
git commit -am "docs(sandbox): OpenSandbox backend section + optional MCP template"
```

---

## Task 3.4：手动端到端验收

无代码改动。

1. 起 `opensandbox-server`
2. `MINI_CC_SANDBOX_BACKEND=opensandbox MINI_CC_REPL_BACKEND=opensandbox ./run`
3. Agent 调 `/repl python` 跑 `import sys; sys.version`
4. 再跑 `x = 42`，再跑 `print(x)` → 预期输出 42（验证 stateful context）
5. 跑 `/mcp` 不应自动出现 opensandbox 工具（默认不预置）；手动复制模板到项目 `.mini_cc/.mcp.json` 后重启，`/tools` 应看到 `mcp__opensandbox__sandbox_create` 等

### Phase 3 完成 commit

```bash
git tag p6-done
```

---

# 风险与未决项

| 风险 | 影响 | 缓解 |
|---|---|---|
| OpenSandbox lifecycle server 版本漂移（field 改名） | runtime 调用突然失败 | 在 `is_available` 后顺手 GET `/openapi.json` 校验关键路径存在；版本不匹配警告 |
| SSE 解析在 chunked 编码下边界问题 | exec 输出截断或乱码 | Phase 1 Task 1.5 已用 `\n\n` 分事件；真实 server 测试时如果出问题再切 httpx |
| `asyncio.run` 在 mini_cc 已经处于 FastAPI worker 的 event loop 里时炸 | REPL 工具调用失败 | Phase 1 全程同步 urllib，不引入 async |
| metadata 字段长度限制（值 ≤63 字符） | 长 tid 被截断导致冲突 | `_find_by_tid` 前 hash 长 tid（SHA1 前 12 位） |
| K8s runtime 下 host mount 不可用 | tenant 工作区挂载失败 | Phase 2 MountSpec 显式区分；`HostMount` 在 K8s 后端下 raise 清晰错误 |
| MCP server 进程内状态丢失 | stdio 模式重启后丢失 sandbox 句柄 | 文档明确说明：MCP 路径仅适合 ad-hoc 任务，per-tenant 持久容器走主 HTTP adapter |

# 不在本计划范围内（YAGNI）

- gVisor / Kata / Firecracker 等 secure runtime：spec 里 `[secure_runtime]` 配置项存在，但 mini_cc 不暴露切换（让 OpenSandbox server 自己配）
- Snapshot/restore：spec 有完整支持，但 mini_cc 目前没有"暂停租户工作区"用例
- Ingress gateway 的 secure access token：mini_cc 走 server-side，不直接面向外部用户
- 多 SDK 语言（Java/Go/JS）：Python 是 mini_cc 唯一宿主语言
- **LSP（Language Server Protocol）**：OpenSandbox 全仓库无 LSP 原生支持（仅代码执行/FS/命令执行/生命周期）。mini_cc 当前 agent 工具集（read/write/edit/grep/glob）已覆盖代码导航需求，LSP 是给 human-in-the-loop 编辑器的能力。未来若需要，方案是「沙箱内起 pyright/gopls/tsserver，用 `sandbox_get_endpoint` 暴露端口，mini_cc 外部连过去」—— 留作 P7+ 候选

---

**Plan 完成标记：** 三阶段全部 commit + 手动验收通过后，更新 `memory/project_mini_cc.md` 记录 P6 已 ship。
