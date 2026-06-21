# Phase G:可配置的容器级 sandbox 隔离(多租户)

## 背景

Phase A–F 已交付(397 后端测试)。现有 `SubprocessSandbox` 把每个
tool 操作锁在 per-project 根目录 + env 白名单 + 命令策略扫描后面。
这套模型是**进程级**的 defense-in-depth —— 但巧妙的 shell 逃逸
(符号链接技巧、`LD_PRELOAD`、内核漏洞)仍然能碰到 host 资源。
Phase G 在其之上加**第二道**隔离层:每个 tool 的 `execute()`/`git()`
调用都跑在 per-tenant Docker 容器里,这样即使成功从 project
workspace 逃出,也只到该 tenant 的容器(和 bind-mount 进去的
tenant workspace),碰不到 host 或其他 tenant。

威胁模型:**(c) 两层都要** —— per-tenant 容器阻止跨 tenant 泄漏
(即使 host 侧路径隔离失效);容器内的 project-root 限制(已由
`SubprocessSandbox` 在 bind-mount 的 fs 上强制)阻止同一 tenant 内
跨 project 泄漏。

向后兼容:默认行为不变。若既没有 `MINI_CC_SANDBOX_DEFAULT=container`
也没有任何带 `enabled = true` 的 `tenants/{tid}/sandbox.toml`,所有
tenant 继续用 `SubprocessSandbox`,跟以前一模一样。

## 方案(brainstorm 锁定)

- **运行时**:Docker Engine CLI(无 SDK 依赖)。dev = Windows/WSL2
  上的 Docker Desktop;prod = Linux + Docker Engine。
- **威胁模型**:per-tenant 容器 **加** 容器内 project root 限制。
- **生命周期**:per-tenant 长驻容器(`sleep infinity`),每次 tool
  调用 `docker exec`。懒启动 —— 首次 `execute()`/`git()` 时才拉起。
- **workspace 存储**:bind-mount 整个 tenant 的 projects 目录 →
  容器内 `/workspaces/`。每个 project 是 `/workspaces/{pid}/`。
- **镜像**:repo 自带 `mini_cc/sandbox/Dockerfile` →
  `mini_cc-sandbox:latest`(python:3.12-slim + git + ripgrep + node 20
  + build-essential)。**不注入 venv** —— agent 自己 `!python -m venv
  .venv`,跟开发者在 fresh VM 上一样。
- **网络**:默认 `--network=none`;tenant 通过 `network = "bridge"`
  opt-in(需要 `npm install` 等场景)。
- **配置**:双层。Server env `MINI_CC_SANDBOX_DEFAULT=subprocess|container`
  (默认 `subprocess`)设全局;`tenants/{tid}/sandbox.toml` 里
  `enabled = true` 覆盖单 tenant。
- **sandbox 切分**:只有 `execute()`/`git()` 走容器。文件操作
  (`read`/`write`/`edit`/`glob`/`grep`)留在 host —— tenant 隔离
  已经在 `ProjectManager` 路径层做掉;容器化 fs 会给一次 turn 几十次
  读各加 ~30ms 开销。
- **失败模式**:fail-fast。如果 `enabled=true` 但 docker 缺失,
  project-open 调用抛错(返回 503),**不**静默回退到 subprocess
  (静默回退等于隔离失效,比直接报错更危险)。

## 文件清单

### `mini_cc/sandbox/runtime.py`(新,~120 行)

```python
class ContainerRuntime(Protocol):
    def is_available(self) -> bool: ...
    def ensure_running(self, *, name, image, mount, network,
                       cpu_quota=None, memory=None) -> None: ...
    def exec(self, *, name, workdir, command,
             timeout, env) -> subprocess.CompletedProcess: ...
    def status(self, name) -> str: ...
    def stop(self, name) -> None: ...
    def remove(self, name) -> None: ...
    def list_managed(self, prefix="mini_cc-") -> list[str]: ...
    def build_image(self, context_dir, tag) -> None: ...

class DockerRuntime:
    """所有 docker 调用走 subprocess;无 SDK 依赖。"""
    def is_available(self) -> bool: ...
    def ensure_running(self, *, name, image, mount, network, ...):
        # status 检查 → "running" 直接返回;"exited" docker start;
        # "missing" docker run -d --restart=unless-stopped ...
    def exec(self, *, name, workdir, command, timeout, env):
        # docker exec -w {workdir} [-e K=V ...] {name} sh -c "{cmd}"
        # (或 list 形式 {name} {cmd...},用于 git)
    def status(self, name) -> str: ...
```

测试替身 `FakeRuntime` 在 `tests/test_phaseG_sandbox_container.py` 里,
把每次调用记录进 `.calls: list[tuple]`。**没有任何测试依赖真 docker
daemon**。

### `mini_cc/sandbox/config.py`(新,~70 行)

```python
@dataclass
class ContainerConfig:
    enabled: bool = False
    network: str = "none"              # "none" | "bridge"
    image: str = "mini_cc-sandbox:latest"
    cpu_quota: str | None = None       # "1.0"
    memory_limit: str | None = None    # "512m"

def server_default_kind() -> str:
    """MINI_CC_SANDBOX_DEFAULT=subprocess|container(默认 subprocess)。"""

def load_tenant_config(tid: str, tenants_dir: Path) -> ContainerConfig | None:
    """读 tenants/{tid}/sandbox.toml。文件缺失返回 None。"""

def resolve_kind(tid, tenants_dir) -> tuple[str, ContainerConfig | None]:
    """返回 ('subprocess', None) 或 ('container', cfg)。"""
```

`sandbox.toml` 是**冷加载**:首次访问时缓存在
`ProjectManager._sandbox_configs[tid]`,改文件需要重启 server。热加载
意味着重建容器,违反"长驻容器"假设。

### `mini_cc/sandbox/manager.py`(新,~80 行)

```python
class TenantContainerManager:
    """持有 per-tenant 容器生命周期。每 tid 一个实例。"""

    def __init__(self, tid: str, host_projects_dir: Path,
                 config: ContainerConfig, runtime: ContainerRuntime):
        self.tid = tid
        self.container_name = _container_name(tid)  # sanitize 后
        self.host_projects_dir = host_projects_dir
        self.config = config
        self.runtime = runtime

    def ensure_running(self) -> None:
        """幂等:status 检查 → 按需 run/start。"""

    def exec(self, *, workdir, command, timeout, env):
        self.ensure_running()
        return self.runtime.exec(name=self.container_name, workdir=workdir,
                                  command=command, timeout=timeout, env=env)

    def stop(self) -> None: ...
```

`_container_name(tid)` 把 tid sanitize 成
`[A-Za-z0-9][A-Za-z0-9_.-]*`,加 `mini_cc-` 前缀,截断到 63 字符。
Server lifespan 用 `list_managed()` 找回所有 `mini_cc-*` 容器,这样
重启后的 server 能清理上次崩溃残留的容器。

### `mini_cc/sandbox/container.py`(新,~70 行)

```python
class ContainerSandbox:
    """Sandbox 协议实现:fs 走 host,exec/git 走容器。"""

    def __init__(self, project_id, project_root, policy,
                 container_mgr: TenantContainerManager):
        self.project_id = project_id
        self.project_root = project_root
        self.policy = policy
        self._fs = SubprocessSandbox(project_id, project_root, policy)
        self._mgr = container_mgr

    # fs:委托给内嵌的 SubprocessSandbox
    def resolve_path(self, rel): return self._fs.resolve_path(rel)
    def validate_path(self, p):  return self._fs.validate_path(p)
    def read(self, *a, **kw):    return self._fs.read(*a, **kw)
    def write(self, *a, **kw):   return self._fs.write(*a, **kw)
    def edit(self, *a, **kw):    return self._fs.edit(*a, **kw)
    def glob(self, *a, **kw):    return self._fs.glob(*a, **kw)
    def grep(self, *a, **kw):    return self._fs.grep(*a, **kw)

    # exec/git:走容器
    def execute(self, command, *, timeout=120, env=None):
        violations = self.policy.scan_command(command)
        if violations: raise CommandBlockedError(violations)
        try:
            return self._mgr.exec(
                workdir=f"/workspaces/{self.project_id}",
                command=command, timeout=timeout,
                env=self._filtered_env(env))
        except FileNotFoundError:
            raise DockerMissingError(...)

    def git(self, args, *, timeout=60):
        v = self.policy.check_git_args(args)
        if v: raise CommandBlockedError([v])
        return self._mgr.exec(
            workdir=f"/workspaces/{self.project_id}",
            command=["git"] + [str(a) for a in args],
            timeout=timeout, env=self._filtered_env(None))

    def _filtered_env(self, extra):
        # 复用 SubprocessSandbox 的 env 白名单
        env = {k: v for k, v in os.environ.items()
               if k in self.policy.allowed_env}
        env["HOME"] = f"/workspaces/{self.project_id}"
        if extra: env.update(extra)
        return env
```

host 侧 `_fs` SubprocessSandbox 负责保证 fs 路径不出 `project_root`。
容器的 `workdir=/workspaces/{pid}` 让容器内 cwd 跟 host 对齐。`HOME`
设成容器内 project 目录,这样依赖 `$HOME` 的工具(git config、
npm cache)落进 workspace。

### `mini_cc/sandbox/Dockerfile`(新,~30 行)

```dockerfile
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        bash git git-lfs ca-certificates curl unzip \
        ripgrep fd-find file less vim-tiny \
        build-essential \
    && ln -s /usr/bin/fdfind /usr/local/bin/fd \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -m -u 1000 agent
USER agent
WORKDIR /workspaces
CMD ["sleep", "infinity"]
```

没有 venv,没有项目依赖。想要隔离 Python 依赖的 agent 自己跑
`!python -m venv .venv && .venv/bin/pip install ...` —— 跟开发者在
fresh VM 上一样的工作流。

### `mini_cc/projects/manager.py`(改,+20 行)

```python
class ProjectManager:
    def __init__(self, root, *, metrics=None,
                 sandbox_factory: Callable[[str, str, Path, Policy], Sandbox]
                     | None = None):
        ...
        self._sandbox_factory = sandbox_factory or _default_sandbox_factory

    def get(self, pid) -> Project:
        # 现有逻辑,但 sandbox = self._sandbox_factory(tid, pid, ws, policy)
```

默认 factory 返回 `SubprocessSandbox`。`cmd_serve` 构建 server 时,
构造一个闭包 factory,调用 `resolve_kind(tid, tenants_dir)`,根据结果
返回 `SubprocessSandbox` 或 `ContainerSandbox`。

### `mini_cc/server/app.py`(改,+15 行)

```python
@asynccontextmanager
async def lifespan(app):
    yield
    # 先停 sessions,再停容器
    sm: SessionManager = app.state.sm
    for (pid, sid), sess in list(sm._sessions.items()):
        try: sess.stop()
        except Exception: pass
    runtime = getattr(app.state, "container_runtime", None)
    if runtime is not None and runtime.is_available():
        for name in runtime.list_managed():
            try:
                runtime.stop(name); runtime.remove(name)
            except Exception: pass
```

`build_app` 新增 `container_runtime: ContainerRuntime | None = None`
kwarg,存到 `app.state`。

### `mini_cc/server/cli.py`(改,+60 行)

`cmd_serve`:

```python
runtime = DockerRuntime()
if not runtime.is_available():
    runtime = None
    log.warning("docker not available; all tenants use subprocess sandbox")

container_mgrs: dict[str, TenantContainerManager] = {}

def sandbox_factory(tid, pid, ws, policy):
    kind, cfg = resolve_kind(tid, tenants_dir)
    if kind == "subprocess" or runtime is None:
        return SubprocessSandbox(pid, ws, policy)
    mgr = container_mgrs.get(tid)
    if mgr is None:
        host_projects_dir = tenants_dir / tid / "projects"
        mgr = TenantContainerManager(tid, host_projects_dir, cfg, runtime)
        container_mgrs[tid] = mgr
    return ContainerSandbox(pid, ws, policy, mgr)
```

新增 `sandbox` 子命令:

- `python -m mini_cc.server sandbox build-image [--tag ...] [--no-cache]`
- `python -m mini_cc.server sandbox status [--tid TID]`
- `python -m mini_cc.server sandbox stop TID`

### `tests/test_phaseG_sandbox_config.py`(新)

- `MINI_CC_SANDBOX_DEFAULT=container` 时,缺失 tenant 配置 → 容器模式。
- `tenants/{tid}/sandbox.toml enabled=true` 覆盖 server 默认。
- `enabled=false` 回落到 server 默认。
- 非法 `network` 值 → ValueError。
- `cpu_quota`/`memory_limit` 透传。

### `tests/test_phaseG_sandbox_container.py`(新)

`FakeRuntime` 记录每次调用。测试:

- 首次 `execute()` 触发 `ensure_running()`(一次 `run` 调用)。
- 后续 `execute()` 跳过 `run`。
- `status="running"` 短路 `ensure_running`。
- `status="exited"` 触发 `docker start`,不是 `docker run`。
- `write()`/`read()`/`glob()`/`grep()` 不产生任何 runtime 调用。
- 策略违规在任何 runtime 调用前抛错。
- `DockerMissingError` 在 `is_available()` 返回 False 时抛出。
- execute 和 git 的容器 workdir 都是 `/workspaces/{pid}`。
- env 白名单过滤 host env;`HOME` 设为容器内 project 目录。
- `_container_name(tid)` sanitize 特殊字符,遵守 63 字符上限。

### `tests/test_phaseG_sandbox_http.py`(新)

通过 `TestClient(build_app(..., container_runtime=fake_rt,
sandbox_factory=...))` 端到端:

- 没有 `sandbox.toml` + 默认 server env → `fake_rt.calls` 为空。
- `enabled=true` 的 tenant → 驱动 bash tool 触发一次 `exec` 调用。
- 容器 exec 超时 → 返回 `returncode=124`(不抛异常)。
- `enabled=true` 但 `fake_rt.is_available()=False` → project-open
  返回 503 + `sandbox_unavailable` code。

### `mini_cc/README.md` + `.zh.md`(改)

新增"Container sandbox(Phase G)"章节,涵盖:

- 威胁模型概述。
- `MINI_CC_SANDBOX_DEFAULT` env 变量。
- `tenants/{tid}/sandbox.toml` schema + 示例。
- `sandbox build-image` / `status` / `stop` CLI。
- Windows + Docker Desktop 文件共享说明。
- 手动验证清单(build image、驱动一轮对话、验证 `!ls /` 显示
  `/workspaces/...`、验证 `!curl` 在 `--network=none` 下失败、切换
  `network = "bridge"`、重启 server)。

## 验证

```bash
# 1. 单元测试(不依赖 docker daemon)
python -m pytest tests/test_phaseG_sandbox_config.py -v
python -m pytest tests/test_phaseG_sandbox_container.py -v
python -m pytest tests/test_phaseG_sandbox_http.py -v

# 2. 全量(预期 ~430 通过)
python -m pytest tests/ -q

# 3. 构建 image
python -m mini_cc.server sandbox build-image
docker images mini_cc-sandbox

# 4. 手动端到端(需要 docker daemon)
mkdir -p $MINI_CC_DATA_DIR/tenants/t1
echo 'enabled = true' > $MINI_CC_DATA_DIR/tenants/t1/sandbox.toml
python -m mini_cc.server &
# 在 chat 里:!ls /  → 应该看到 /workspaces/...
# 在 chat 里:!curl http://example.com  → 应该失败
# 改 sandbox.toml 设 network = "bridge",重启 server,再试 curl
docker ps | grep mini_cc-t1

# 5. 关停时清理容器
python -m mini_cc.server sandbox status   # server stop 后应为空
```

## Phase G 范围外

- **Per-project sandbox 配置覆盖。** Sandbox 是 tenant 级别的关注点;
  project 级权限继续放在 `permissions.toml`。
- **sandbox.toml 热加载。** 改动需要重启 server。
- **镜像自动 build。** 首次 `enabled=true` 且镜像缺失时直接报错
  (`image_missing`),不自动 build。build 受网络影响、慢,不该阻塞
  HTTP 请求。
- **Podman / gVisor / Firecracker backend。** `ContainerRuntime` 是
  抽象的;目前只实现 Docker。
- **Multi-arch 镜像。** Dockerfile 只 build 当前架构;arm64 用户自行
  `docker buildx build`。
- **Windows native container。** 只跑 Linux container(WSL2 backend)。
- **Rootless Docker / userns-remap。** 只在标准 Docker Engine +
  Docker Desktop 上测过。
- **`docker stats` 指标收集。** `cpu_quota`/`memory_limit` 是硬上限,
  但不向 metrics 注册表报 per-container 用量。属于未来工作。
- **Python venv 注入。** 想要隔离 Python 依赖的 agent 自己创建 venv。
  容器镜像只装系统 Python + node + git。
- **任务队列 / job 模式异步 turn。** HTTP `/send` 继续以 stream 为
  默认。把 `loop.run()` 从 HTTP handler 解耦到 `POST /send?mode=job` +
  `GET /jobs/{job_id}/events` 单独成 **Phase H**(见 Future Work)。
