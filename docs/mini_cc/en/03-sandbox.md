[ < [02](02-storage-projects-sessions.md) ] [ next > ] · [中文版本](../zh/03-sandbox.md)

# 03 — Three-tier Sandbox

> The sandbox is the only mini_cc subsystem with two Protocol layers and
> multiple implementations: `Sandbox` (the contract tools see) sits on top
> of `ContainerRuntime` (the contract a container backend sees). This
> chapter unpacks three things: the **opensandbox → docker → subprocess
> three-tier fallback**, the **MountSpec abstraction**, and the
> **`name=tid` contract**. After reading it you should be able to add a
> gVisor / Firecracker backend without touching a line of upper-layer
> code.

---

## Problem & motivation

s20's sandbox is just `subprocess.run(shell=True)`. That's fine for
teaching, but in production three problems blow up immediately:

1. **Untrusted-code escape**. The model's bash might `curl | sh`, read
   `/etc/passwd`, or fork-bomb. `Policy`'s regex deny-list is a
   defense-in-depth layer, but it won't stop a determined adversary — you
   need to lock the whole execution environment inside a container.
2. **The "container vs process" choice varies by deployment shape**. A
   local dev box wants Docker; a cloud K8s cluster wants OpenSandbox
   (which has PVC, OSSFS, lifecycle management); a CI runner might only
   have subprocess. **None of this should be visible to upper-layer tool
   code** — the `bash` tool just wants to run a command and doesn't care
   whether it's a `docker exec` or an HTTP POST.
3. **"Hard-fail when the container is unavailable" is the wrong default**.
   The original Phase G design was fail-fast: docker daemon down → 503.
   But ops' real requirement is "**silently fall back to subprocess when
   the container is unavailable, and log a DegradeEvent for monitoring**"
   — the service must keep booting.

mini_cc's answer is two interlocking abstractions: the `Sandbox` Protocol
(for tools) and the `ContainerRuntime` Protocol (for container backends),
adapted in the middle by `ContainerSandbox`. Backend selection lives in
`ServerRuntimeContext._build_runtime` with a three-tier fallback:

```
opensandbox  (if OPEN_SANDBOX_* is configured AND /health returns 200)
        │ unavailable
        ▼
docker       (probe_docker finds a daemon)
        │ unavailable
        ▼
None  →  _sandbox_factory silently degrades every container tenant to SubprocessSandbox
```

---

## Design & principles

### 1. The two Protocol layers

```
┌────────────────────────────────────────────────────────────────┐
│ Sandbox Protocol  (the contract tools see)                     │
│   SubprocessSandbox      ← local subprocess; files + cmds on host │
│   ContainerSandbox       ← files stay on host; commands go to container │
│        │_fs: SubprocessSandbox   (embedded; handles fs ops)    │
│        │_mgr: TenantContainerManager                           │
│                └─ runtime: ContainerRuntime  ← backend Protocol │
└────────────────────────────────────────────────────────────────┘
```

**The key division of labor** (easy to get wrong):

| Implementation | File ops (read/write/edit/glob/grep) | Process ops (execute/git) |
|----------------|--------------------------------------|---------------------------|
| `SubprocessSandbox` | host, gated by `Policy` | `subprocess.run` (POSIX sh / bash) |
| `ContainerSandbox` | **still on host** (embeds a `SubprocessSandbox` as `_fs`) | forwarded to `TenantContainerManager.exec` → `runtime.exec` |

> Even with containers enabled, **file reads/writes run on the host**.
> Reason: the tenant's projects dir is bind-mounted into the container at
> `/workspaces/`, so host and container see the same files — file ops
> reuse the host's path-validation logic, which is both faster and safer.
> Only `execute()` / `git()` enter the container.

`ContainerSandbox._filtered_env` (`sandbox/container.py:60`) also does
something important: it rewrites `HOME` to `/workspaces/<project_id>` so
that in-container git config and npm cache land inside the workspace
rather than polluting the image's `/root`.

### 2. The `name=tid` contract

`TenantContainerManager` (`sandbox/manager.py:29`) passes the **tenant id
verbatim** as the `name` argument to the runtime's `ensure_running` /
`exec`. Each runtime decides how to use it:

- `DockerRuntime._docker_name_from_tid(tid)` (`sandbox/runtime.py:33`):
  replaces characters outside `[A-Za-z0-9_.-]` with `_`, prepends
  `mini_cc-`, clips to 63 chars. Produces a legal docker container name.
- `OpenSandboxRuntime`: writes the tid into the sandbox's metadata field
  `mini-cc-tid=<tid>` (`opensandbox_runtime.py:246`) and indexes by it.
  Note the hyphen, not underscore — OpenSandbox spec requires metadata
  keys to be DNS-label compliant.

**Two backends, one tid input, each decides how to consume it.** That's
the core of the P6 Phase 2 refactor: callers **never** pre-sanitize;
they hand the logical identity (tid) straight down.

### 3. The MountSpec abstraction

Different backends have different mount capabilities. Docker only
understands bind mounts; OpenSandbox also understands K8s PVC and OSSFS.
`MountSpec` (`sandbox/config.py:90`) decouples "what to mount" from "how
to mount it":

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

Each runtime translates a MountSpec into its own volume shape:

```
MountSpec(backend=HostMount(path="/host/cache"))
   │
   ├──▶ DockerRuntime:     -v /host/cache:/cache[:ro]
   └──▶ OpenSandboxRuntime: {"host": {"path": "/host/cache"}}

MountSpec(backend=PVCMount(claim_name="data"))
   │
   ├──▶ DockerRuntime:     raise ValueError("only supports HostMount")
   └──▶ OpenSandboxRuntime: {"pvc": {"claimName": "data", ...}}
```

`from_legacy_tuple` (`config.py:98`) lets the old
`(host, container, options)` shape migrate smoothly — the manager layer
doesn't have to change all at once. `_build_volumes`
(`opensandbox_runtime.py:316`) accepts both tuples and MountSpecs;
tuples are converted via `from_legacy_tuple` and then run through the
same translation path.

### 4. Three-tier fallback and auto-degrade

`ServerRuntimeContext._build_runtime` (`server/runtime_context.py:57`)
selects the container backend **once at startup**:

```
MINI_CC_SANDBOX_BACKEND = auto (default) | opensandbox | docker
                │
                ▼
   backend ∈ {opensandbox, auto}?
        │ yes
        ▼
   _try_opensandbox():
     no OPEN_SANDBOX_DOMAIN/API_KEY env → return None (auto silently skips)
     env present → OpenSandboxRuntime.is_available()?
        GET {root_url}/health == 200 means available
        │ available → select OpenSandboxRuntime (no further fallback)
        │ unavailable AND backend=opensandbox → warn + continue down
   ▼
   probe_docker() (osdetect.py)
     Linux/macOS: docker info directly
     Windows: try `wsl docker version` first (30s cold-start), then native docker
        │ available → select DockerRuntime(prefix=avail.argv_prefix)
        │ unavailable → _runtime = None
```

**Two degrade points, different meanings** (`runtime_context.py:111`):

1. **`_build_runtime` selects no backend** (both docker and OS
   unavailable) → `_runtime=None`.
2. **`_sandbox_factory` finds no usable backend while assembling a
   project** (`docker_available=False`) → even if the tenant's
   `sandbox.toml` says `enabled=true`, it **silently falls back** to
   `SubprocessSandbox` and records a `DegradeEvent`:

```python
# mini_cc/server/runtime_context.py:111
def _sandbox_factory(self, tid, pid, ws, policy):
    kind, cfg = resolve_kind(tid, self.tenants_dir)
    if kind == "subprocess" or cfg is None:
        return SubprocessSandbox(pid, ws, policy)
    if not self.docker_available:                   # ← second degrade point
        self.degrades.append(DegradeEvent(
            tenant_id=tid,
            reason="docker unavailable; falling back to subprocess"))
        return SubprocessSandbox(pid, ws, policy)
    mgr = self._container_mgrs.get(tid) or TenantContainerManager(...)
    return ContainerSandbox(pid, ws, policy, mgr)
```

`docker_available` is **deliberately redefined** under the OpenSandbox
backend (`runtime_context.py`): `probe_docker()` returns unavailable for
the OS backend, but as long as `ctx._runtime is not None` (an OS backend
was selected), it counts as "a container backend is available", keeping
the degrade logic correct.

**The tenant-level switch** (`resolve_kind`, `sandbox/config.py:181`)
resolution order: tenant `sandbox.toml` → `MINI_CC_SANDBOX_DEFAULT` env →
`subprocess`.

### 5. The container path of one `execute()` call

```
tool calls ctx.sandbox.execute(cmd)
   │
   ▼
ContainerSandbox.execute (container.py:72)
   ├── Policy.scan_command(cmd)        ← dangerous-command regex gate
   │     hit → raise CommandBlockedError
   ├── (cwd?) rewrite relative cwd as `cd <rel> && cmd`
   └── _filtered_env(env)              ← whitelist + HOME=/workspaces/<pid>
        │
        ▼
   TenantContainerManager.exec (manager.py:57)
   ├── ensure_running()                ← idempotent: status() running → return
   └── runtime.exec(name=tid, workdir=/workspaces/<pid>, command, env)
        │
        ├── DockerRuntime → docker exec -w /workspaces/<pid> <name> sh -c "<cmd>"
        └── OpenSandboxRuntime → three steps:
              1. _find_by_tid(cfg, tid)          ← GET /sandboxes?metadata=mini-cc-tid=<tid>
              2. _get_execd_endpoint(cfg, sid)   ← GET /sandboxes/{id}/endpoints/44772
              3. POST {execd_url}/command {command, cwd, envs}
                 → JSON-per-line stream: stdout/stderr/execution_complete
```

`OpenSandboxRuntime._parse_sse_output` (`opensandbox_runtime.py:371`)
parses this stream: even though the content-type is `text/event-stream`,
execd actually emits **one JSON object per line** (not standard SSE
`event:`/`data:` framing), each carrying a `type` field.

---

## Operation & configuration

### Backend selection

| Variable | Value | Behavior |
|----------|-------|----------|
| `MINI_CC_SANDBOX_BACKEND` | `auto` (default) | try opensandbox first (if env configured), fall back to docker, then None |
| | `opensandbox` | force opensandbox; warn + fall back to docker if unavailable |
| | `docker` | skip opensandbox, go straight to docker |
| `OPEN_SANDBOX_PROTOCOL` | `http` (default) | OS protocol |
| `OPEN_SANDBOX_DOMAIN` | `localhost:8080` (default) | OS lifecycle server address |
| `OPEN_SANDBOX_API_KEY` | (empty) | OS API key (dev-mode servers don't check) |
| `MINI_CC_SANDBOX_DEFAULT` | `subprocess` (default) / `container` | single-tenant default sandbox kind |

### Per-tenant sandbox.toml

Lives at `<data>/tenants/<tid>/sandbox.toml` (cold-loaded; restart to
pick up edits):

```toml
enabled = true
image_tag = "my-registry/sandbox:v2"   # default mini_cc-sandbox:latest
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
python -m mini_cc.server sandbox status [--tid T]   # list all mini_cc-* containers/sandboxes
python -m mini_cc.server sandbox stop <tid>
```

On Windows `probe_docker` first tries `wsl docker version` (30s for cold
start), then native docker (`osdetect.py:107`). The result is cached for
the process lifetime and carries an `argv_prefix` (empty tuple = native
docker; `("wsl",)` = Docker lives inside WSL2).

---

## Verification steps

```bash
# 1. default subprocess: works on any machine
python -m mini_cc.server keygen t1
MINI_CC_DATA_DIR=$PWD/mini_cc_data \
python -m mini_cc.server &
curl -s -X POST http://127.0.0.1:8000/tenants/t1/projects \
     -H "Authorization: Bearer $KEY" -d '{"project_id":"p1"}' >/dev/null
# running the bash tool in chat → goes through SubprocessSandbox
curl -N -X POST http://127.0.0.1:8000/tenants/t1/projects/p1/sessions/s1/send \
     -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
     -d '{"user_input":"run: uname -a"}'

# 2. force docker backend (no docker installed → auto-degrades to subprocess, no error)
MINI_CC_SANDBOX_BACKEND=docker \
MINI_CC_DATA_DIR=$PWD/mini_cc_data \
python -m mini_cc.server
# logs: probe_docker fails, records a DegradeEvent; service still boots

# 3. enable the container sandbox for one tenant
mkdir -p $PWD/mini_cc_data/tenants/t1
cat > $PWD/mini_cc_data/tenants/t1/sandbox.toml <<'EOF'
enabled = true
image_tag = "mini_cc-sandbox:latest"
network = "none"
EOF
python -m mini_cc.server sandbox build-image           # build the image
python -m mini_cc.server sandbox status --tid t1       # should show mini_cc-t1 container

# 4. OpenSandbox backend: set OPEN_SANDBOX_* env then start
OPEN_SANDBOX_PROTOCOL=http \
OPEN_SANDBOX_DOMAIN=osb.internal:8080 \
OPEN_SANDBOX_API_KEY=$OSB_KEY \
MINI_CC_SANDBOX_BACKEND=auto \
python -m mini_cc.server
# _try_opensandbox GETs http://osb.internal:8080/health; 200 → OS backend selected
```

```bash
# unit tests: three-tier fallback + auto-degrade + name=tid contract
python -m pytest tests/test_sandbox_runtime.py tests/test_container_sandbox.py -q
# FakeRuntime is injected, no real docker daemon required
```

---

## Common pitfalls / debugging

1. **"I enabled the container — why does `read` still hit the host?"** By
   design. `ContainerSandbox` delegates all file ops to an embedded
   `SubprocessSandbox` (`container.py:38-57`). Only `execute`/`git`
   enter the container. The bind-mount guarantees both sides see the
   same files.
2. **`wsl docker version` times out on Windows.** WSL2's first cold
   start can take 10+ seconds. `osdetect.py:119` allows 30s. If WSL
   isn't installed, it falls back to native docker; only when both are
   missing does it report unavailable.
3. **Editing `sandbox.toml` has no effect.** Config is **cold-loaded** —
   `ProjectManager` caches per tenant; restart the server to pick up
   edits. `resolve_kind` is called only at assembly time.
4. **`DockerRuntime only supports HostMount`.** Your `extra_mounts`
   uses a PVC shape but the backend is docker. `runtime.py:131` rejects
   it explicitly. Either switch to the OpenSandbox backend or change the
   mount to HostMount.
5. **`list_managed` ignores the prefix arg under the OS backend.**
   OpenSandbox sandboxes aren't filtered by container-name prefix;
   they're filtered by the `managed-by=mini-cc` metadata field
   (`opensandbox_runtime.py:159`). The CLI `sandbox status` works for
   both backends, but the underlying semantics differ.

---

## Further reading

- Sibling chapters: [01 — Overview & Architecture](01-overview.md) ·
  [02 — Storage, Projects, Sessions](02-storage-projects-sessions.md)
- Conceptual: [s03 — Todo Write](../../en/s03-todo-write.md)
  (make_permission_hook's deny-list + destructive gate works alongside Policy)
- Source: `mini_cc/sandbox/base.py`, `runtime.py`, `container.py`,
  `manager.py`, `config.py`, `opensandbox_runtime.py`, `osdetect.py`,
  `server/runtime_context.py`
- Advanced: `docs/mini_cc/container-sandbox.md` (full P5 container-sandbox
  manual), `docs/mini_cc/stateful-repl.md` (OpenSandbox Jupyter context
  reuse), `mini_cc/ARCH.zh.md` §3 (full Mermaid diagrams)
