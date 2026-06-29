[ < [02](02-storage-projects-sessions.md) ] [ next > ] · [English version](../en/03-sandbox.md)

# 03 — 三层沙箱

> 沙箱是 mini_cc 唯一一个「两层 Protocol + 多个实现」的子系统:`Sandbox`
> (对工具的承诺)下面挂着 `ContainerRuntime`(对容器后端的承诺)。本章拆
> 三件事:**opensandbox → docker → subprocess 的三级回退**、**MountSpec
> 抽象**、**`name=tid` 契约**。读完你应该能自己加一个 gVisor / Firecracker
> 后端而不动一行上层代码。

---

## 问题与动机

`s20` 的沙箱就是「`subprocess.run(shell=True)`」。在教学场景里够了,但生产里
立刻爆三个问题:

1. **不受信代码逃逸**。模型写的 bash 可能 `curl | sh`、读 `/etc/passwd`、
   fork bomb。`Policy` 的正则黑名单是 defense-in-depth 一层,但挡不住有决心
   的对手——你需要把整个执行环境关进容器。
2. **多租户的「容器 vs 进程」选择是按部署形态变的**。本地开发机想用 Docker;
   云上 K8s 集群想用 OpenSandbox(它有 PVC、OSSFS、生命周期管理);CI 可能
   干脆只有 subprocess。**这些不该让上层工具代码知道**——`bash` 工具只想执行
   命令,不关心是 docker exec 还是 HTTP POST。
3. **「容器不可用时硬失败」是错的默认**。原 Phase G 设计是 fail-fast:
   docker daemon 挂了就 503。但运维的真实需求是「**容器不可用时静默回退到
   subprocess,并在监控里记一条 DegradeEvent**」,服务继续能起。

mini_cc 的回答是两个咬合的抽象:`Sandbox` Protocol(给工具用)和
`ContainerRuntime` Protocol(给容器后端用),中间靠 `ContainerSandbox` 适配。
后端选择放在 `ServerRuntimeContext._build_runtime`,三级 fallback:

```
opensandbox(若配了 OPEN_SANDBOX_* 且 /health 200)
        │ 不可用
        ▼
docker(probe_docker 探到 daemon)
        │ 不可用
        ▼
None  →  _sandbox_factory 对每个容器租户静默回退到 SubprocessSandbox
```

---

## 设计与原理

### 1. 两层 Protocol

```
┌────────────────────────────────────────────────────────────────┐
│ Sandbox Protocol  (对工具的承诺)                              │
│   SubprocessSandbox      ← 本地子进程,文件 + 命令都在宿主机    │
│   ContainerSandbox       ← 文件留宿主机,命令转给容器          │
│        │_fs: SubprocessSandbox   (内嵌,负责文件操作)         │
│        │_mgr: TenantContainerManager                           │
│                └─ runtime: ContainerRuntime  ← 后端 Protocol  │
└────────────────────────────────────────────────────────────────┘
```

**关键分工**(容易误解):

| 实现 | 文件操作 (read/write/edit/glob/grep) | 进程操作 (execute/git) |
|------|--------------------------------------|------------------------|
| `SubprocessSandbox` | 宿主机,受 `Policy` 约束 | `subprocess.run`(POSIX sh / bash) |
| `ContainerSandbox` | **仍走宿主机**(内嵌一个 `SubprocessSandbox` 做 `_fs`) | 转给 `TenantContainerManager.exec` → `runtime.exec` |

> 即使用了容器,**文件读写还是在宿主机跑**。原因是租户的 projects 目录
> bind-mount 到容器 `/workspaces/`,宿主和容器看到同一份文件——所以文件操作
> 复用宿主机的路径校验逻辑,既快又安全。只有 `execute()` / `git()` 才进容器。

`ContainerSandbox._filtered_env`(`sandbox/container.py:60`)还做了一件重要
的事:把 `HOME` 改写成 `/workspaces/<project_id>`,这样容器里跑的 git config、
npm cache 都落在工作区内,不会污染容器镜像的 `/root`。

### 2. `name=tid` 契约

`TenantContainerManager`(`sandbox/manager.py:29`)把**租户 id 原样**作为 `name`
传给 runtime 的 `ensure_running` / `exec`。每个 runtime 自己决定怎么用它:

- `DockerRuntime._docker_name_from_tid(tid)`(`sandbox/runtime.py:33`):把 tid 里
  非 `[A-Za-z0-9_.-]` 的字符替换成 `_`,加 `mini_cc-` 前缀,裁到 63 字符。
  生成合法 docker 容器名。
- `OpenSandboxRuntime`:把 tid 写进 sandbox 的 metadata 字段
  `mini-cc-tid=<tid>`(`opensandbox_runtime.py:246`),靠它做索引。注意 key
  用连字符不用下划线——OpenSandbox spec 要求 metadata key 符合 DNS label 规则。

**两种后端用同一个 tid 入参,各自决定怎么用**。这就是 P6 Phase 2 重构的核心:
调用方**不要**预先 sanitize,把逻辑身份(tid)直接传下去。

### 3. MountSpec 抽象

不同后端的挂载能力不同。Docker 只懂 bind mount;OpenSandbox 还懂 K8s PVC、
OSSFS。`MountSpec`(`sandbox/config.py:90`)把「挂什么」和「怎么挂」解耦:

```python
# mini_cc/sandbox/config.py:64
@dataclass(frozen=True) class HostMount:    path: str
@dataclass(frozen=True) class PVCMount:     claim_name, create_if_not_exists, ...
@dataclass(frozen=True) class OSSFSMount:   bucket, endpoint, ak, sk
MountBackend = HostMount | PVCMount | OSSFSMount

@dataclass(frozen=True)
class MountSpec:
    name: str
    mount_path: str
    backend: MountBackend
    read_only: bool = False
    sub_path: str | None = None
```

每个 runtime 把 MountSpec 翻译成自己的 volume 形态:

```
MountSpec(backend=HostMount(path="/host/cache"))
   │
   ├──▶ DockerRuntime:   -v /host/cache:/cache[:ro]
   └──▶ OpenSandboxRuntime: {"host": {"path": "/host/cache"}}

MountSpec(backend=PVCMount(claim_name="data"))
   │
   ├──▶ DockerRuntime:   raise ValueError("only supports HostMount")
   └──▶ OpenSandboxRuntime: {"pvc": {"claimName": "data", ...}}
```

`from_legacy_tuple`(`config.py:98`)让老的 `(host, container, options)` 形态
平滑迁移——manager 层不必一次性全改。`_build_volumes`
(`opensandbox_runtime.py:316`)同时接受 tuple 和 MountSpec,tuple 经
`from_legacy_tuple` 转换后走同一条翻译路径。

### 4. 三级 fallback 与自动降级

`ServerRuntimeContext._build_runtime`(`server/runtime_context.py:57`)在
**启动时一次性**选定容器后端:

```
MINI_CC_SANDBOX_BACKEND = auto(默认) | opensandbox | docker
                │
                ▼
   backend ∈ {opensandbox, auto}?
        │ 是
        ▼
   _try_opensandbox():
     无 OPEN_SANDBOX_DOMAIN/API_KEY env → 返回 None(auto 模式静默跳过)
     有 env → OpenSandboxRuntime.is_available()?
        GET {root_url}/health == 200 才算可用
        │ 可用 → 选中 OpenSandboxRuntime(后续不再往下)
        │ 不可用 且 backend=opensandbox → 警告 + 继续往下
   ▼
   probe_docker()(osdetect.py)
     Linux/macOS: docker info 直接探
     Windows: 先试 wsl docker version(冷启动 30s),再试 native docker
        │ 可用 → 选中 DockerRuntime(prefix=avail.argv_prefix)
        │ 不可用 → _runtime = None
```

**两个降级点,意义不同**(`runtime_context.py:111`):

1. **`_build_runtime` 选不到后端**(docker 和 OS 都不可用)→ `_runtime=None`。
2. **`_sandbox_factory` 装配某个项目时发现没有可用后端**
   (`docker_available=False`)→ 即使租户 `sandbox.toml` 写了 `enabled=true`,
   也**静默回退**到 `SubprocessSandbox`,并记一条 `DegradeEvent`:

```python
# mini_cc/server/runtime_context.py:111
def _sandbox_factory(self, tid, pid, ws, policy):
    kind, cfg = resolve_kind(tid, self.tenants_dir)
    if kind == "subprocess" or cfg is None:
        return SubprocessSandbox(pid, ws, policy)
    if not self.docker_available:                   # ← 第二降级点
        self.degrades.append(DegradeEvent(
            tenant_id=tid,
            reason="docker unavailable; falling back to subprocess"))
        return SubprocessSandbox(pid, ws, policy)
    mgr = self._container_mgrs.get(tid) or TenantContainerManager(...)
    return ContainerSandbox(pid, ws, policy, mgr)
```

`docker_available` 在 OpenSandbox 后端下被**有意改写过语义**
(`runtime_context.py`):`probe_docker()` 对 OS 后端返回 unavailable,但只要
`ctx._runtime is not None`(选到了 OS),就视作「容器后端可用」,让降级逻辑
判断正确。

**租户级开关**(`resolve_kind`,`sandbox/config.py:181`)解析顺序:租户
`sandbox.toml` → `MINI_CC_SANDBOX_DEFAULT` env → `subprocess`。

### 5. 一次 `execute()` 的容器路径

```
tool 调 ctx.sandbox.execute(cmd)
   │
   ▼
ContainerSandbox.execute(container.py:72)
   ├── Policy.scan_command(cmd)        ← 危险命令正则拦截
   │     命中 → raise CommandBlockedError
   ├── (cwd?) 把相对 cwd 转成 `cd <rel> && cmd`
   └── _filtered_env(env)              ← 白名单 + HOME=/workspaces/<pid>
        │
        ▼
   TenantContainerManager.exec(manager.py:57)
   ├── ensure_running()                ← 幂等:status() 已 running 就直接返回
   └── runtime.exec(name=tid, workdir=/workspaces/<pid>, command, env)
        │
        ├── DockerRuntime → docker exec -w /workspaces/<pid> <name> sh -c "<cmd>"
        └── OpenSandboxRuntime → 三步:
              1. _find_by_tid(cfg, tid)          ← GET /sandboxes?metadata=mini-cc-tid=<tid>
              2. _get_execd_endpoint(cfg, sid)   ← GET /sandboxes/{id}/endpoints/44772
              3. POST {execd_url}/command {command, cwd, envs}
                 → JSON-per-line 流:stdout/stderr/execution_complete
```

`OpenSandboxRuntime._parse_sse_output`(`opensandbox_runtime.py:371`)解析这个
流:虽然 content-type 是 `text/event-stream`,execd 实际是**每行一个 JSON
对象**(不是标准 SSE 的 `event:`/`data:` 帧),每行带 `type` 字段。

---

## 操作与配置

### 后端选择

| 变量 | 值 | 行为 |
|------|-----|------|
| `MINI_CC_SANDBOX_BACKEND` | `auto`(默认) | 先试 opensandbox(若配了 env),失败回落 docker,再失败 None |
| | `opensandbox` | 强制 opensandbox;不可用警告 + 回落 docker |
| | `docker` | 跳过 opensandbox,直接 docker |
| `OPEN_SANDBOX_PROTOCOL` | `http`(默认) | OS 协议 |
| `OPEN_SANDBOX_DOMAIN` | `localhost:8080`(默认) | OS lifecycle server 地址 |
| `OPEN_SANDBOX_API_KEY` | (空) | OS API key(dev 模式 server 不校验) |
| `MINI_CC_SANDBOX_DEFAULT` | `subprocess`(默认) / `container` | 单租户默认沙箱 kind |

### 租户级 sandbox.toml

放在 `<data>/tenants/<tid>/sandbox.toml`(冷加载,改了要重启):

```toml
enabled = true
image_tag = "my-registry/sandbox:v2"   # 默认 mini_cc-sandbox:latest
network = "none"                        # "none" | "bridge"
dockerfile_path = "./sandbox/Dockerfile.x"
cpu_quota = "1.5"                       # docker --cpus
memory_limit = "512m"                   # docker --memory
apt_packages = ["ffmpeg", "imagemagick"]
pip_packages  = ["numpy", "pandas"]
node_packages = ["typescript"]
[[extra_mounts]]
host = "/host/cache"
container = "/cache"
options = "ro"
```

### CLI

```bash
python -m mini_cc.server sandbox build-image [--tag T] [--dockerfile P]
python -m mini_cc.server sandbox status [--tid T]   # 列出所有 mini_cc-* 容器/sandbox
python -m mini_cc.server sandbox stop <tid>
```

`probe_docker` 在 Windows 上会先试 `wsl docker version`(冷启动给 30s),再试
native docker(`osdetect.py:107`)。探测结果缓存在进程生命周期内,带一个
`argv_prefix`(空 tuple = native docker;`("wsl",)` = Docker 在 WSL2 里)。

---

## 验证步骤

```bash
# 1. 默认 subprocess:任何机器都能跑
python -m mini_cc.server keygen t1
MINI_CC_DATA_DIR=$PWD/mini_cc_data \
python -m mini_cc.server &
curl -s -X POST http://127.0.0.1:8000/tenants/t1/projects \
     -H "Authorization: Bearer $KEY" -d '{"project_id":"p1"}' >/dev/null
# 在 chat 里跑 bash 工具 → 走 SubprocessSandbox
curl -N -X POST http://127.0.0.1:8000/tenants/t1/projects/p1/sessions/s1/send \
     -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
     -d '{"user_input":"run: uname -a"}'

# 2. 强制 docker 后端(没装 docker → 自动降级到 subprocess,不报错)
MINI_CC_SANDBOX_BACKEND=docker \
MINI_CC_DATA_DIR=$PWD/mini_cc_data \
python -m mini_cc.server
# 看日志:probe_docker 失败,记录 DegradeEvent;服务正常起

# 3. 给某租户开容器沙箱
mkdir -p $PWD/mini_cc_data/tenants/t1
cat > $PWD/mini_cc_data/tenants/t1/sandbox.toml <<'EOF'
enabled = true
image_tag = "mini_cc-sandbox:latest"
network = "none"
EOF
python -m mini_cc.server sandbox build-image           # 构建镜像
python -m mini_cc.server sandbox status --tid t1       # 应见 mini_cc-t1 容器

# 4. OpenSandbox 后端:配 OPEN_SANDBOX_* env 后启动
OPEN_SANDBOX_PROTOCOL=http \
OPEN_SANDBOX_DOMAIN=osb.internal:8080 \
OPEN_SANDBOX_API_KEY=$OSB_KEY \
MINI_CC_SANDBOX_BACKEND=auto \
python -m mini_cc.server
# _try_opensandbox 会 GET http://osb.internal:8080/health;200 → 选中 OS 后端
```

```bash
# 单元测试:三级 fallback + 自动降级 + name=tid 契约
python -m pytest tests/test_sandbox_runtime.py tests/test_container_sandbox.py -q
# FakeRuntime 注入,不打真实 docker daemon
```

---

## 常见坑与调试

1. **「我用容器了,为什么 `read` 还是读宿主机?」** 这是设计。`ContainerSandbox`
   的文件操作全部委托给内嵌的 `SubprocessSandbox`(`container.py:38-57`)。
   只有 `execute`/`git` 进容器。bind-mount 保证两边看到同一份文件。
2. **Windows 上 `wsl docker version` 超时。** WSL2 冷启动第一次可能要十几秒。
   `osdetect.py:119` 给了 30s timeout。如果你 WSL 没装,会回落到 native
   docker;两个都没有才报 unavailable。
3. **改了 `sandbox.toml` 没生效。** 配置是**冷加载**的——`ProjectManager` 缓存
   租户配置,改文件要重启 server。`resolve_kind` 只在装配时调一次。
4. **`DockerRuntime only supports HostMount`。** 你的 `extra_mounts` 用了 PVC
   形态但后端是 docker。`runtime.py:131` 显式拒绝。要么换 OpenSandbox 后端,
   要么把 mount 改回 HostMount。
5. **`list_managed` 在 OS 后端下忽略 prefix 参数。** OpenSandbox 的 sandbox
   不是按容器名前缀过滤,而是按 metadata `managed-by=mini-cc` 过滤
   (`opensandbox_runtime.py:159`)。CLI `sandbox status` 对两种后端都work,
   但底层语义不同。

---

## 延伸阅读

- 兄弟章:[01 — 总览与架构](01-overview.md) ·
  [02 — 存储、项目与会话](02-storage-projects-sessions.md)
- 概念章:[s03 — Todo Write](../../zh/s03-todo-write.md)
  (make_permission_hook 的 deny-list + destructive 拦截与 Policy 协同)
- 源码:`mini_cc/sandbox/base.py`、`runtime.py`、`container.py`、`manager.py`、
  `config.py`、`opensandbox_runtime.py`、`osdetect.py`、`server/runtime_context.py`
- 进阶:`docs/mini_cc/container-sandbox.md`(P5 容器沙箱完整手册)、
  `docs/mini_cc/stateful-repl.md`(OpenSandbox Jupyter context 复用)、
  `mini_cc/ARCH.zh.md` §3(Mermaid 全图)
