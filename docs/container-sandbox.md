# Container Sandbox (P5)

Mini_cc's *soft sandbox* by default runs every bash / git / `execute_code`
call in a child subprocess of the host Python process — fast, zero setup,
but isolated only by path whitelisting and a dangerous-command policy.
P5 adds an opt-in **per-tenant Docker container** that hardens the
boundary: each tenant gets one long-running container, and every
project under that tenant `docker exec`s into it.

The original design called for fail-fast when Docker wasn't available.
The shipped implementation honours a stronger user requirement —
**auto-degrade**: if Docker is unreachable, container-enabled tenants
silently fall back to `SubprocessSandbox` and a `DegradeEvent` is
recorded for metrics.

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│                     ServerRuntimeContext                     │
│  ┌────────────────────┐   ┌───────────────────────────────┐  │
│  │ docker_available   │   │ _container_mgrs[tid]          │  │
│  │ (probe once/boot)  │   │  TenantContainerManager cache │  │
│  └────────────────────┘   └───────────────────────────────┘  │
└──────────────────────────────────────────────────────────────┘
              │
              │  _sandbox_factory(tid, pid, ws, policy)
              ▼
   ┌─────────────────────────────────────────┐
   │ resolve_kind(tid, tenants_dir)          │
   │   → ("subprocess", None)                │
   │   → ("container", ContainerConfig)      │
   └─────────────────────────────────────────┘
              │
              ├── kind=subprocess ───────────► SubprocessSandbox
              │
              └── kind=container:
                     ├── docker_available=False ─► SubprocessSandbox
                     │                                + DegradeEvent
                     └── docker_available=True  ─► ContainerSandbox
                                                       │
                                              TenantContainerManager.ensure()
                                                       │
                                              DockerRuntime.start/exec/stop
```

Key files:

| File                                | Role                                             |
| ----------------------------------- | ------------------------------------------------ |
| `mini_cc/sandbox/osdetect.py`       | WSL2 detection on Windows                        |
| `mini_cc/sandbox/config.py`         | `ContainerConfig` + `resolve_kind`               |
| `mini_cc/sandbox/imagebuild.py`     | Builds the tenant image from Dockerfile + extras |
| `mini_cc/sandbox/runtime.py`        | `DockerRuntime` (`FakeRuntime` for tests)        |
| `mini_cc/sandbox/manager.py`        | `TenantContainerManager` — one per tenant        |
| `mini_cc/sandbox/container.py`      | `ContainerSandbox` — the Sandbox impl            |
| `mini_cc/sandbox/Dockerfile`        | Base image (Python + Node + git)                 |
| `mini_cc/server/runtime_context.py` | `ServerRuntimeContext` + degrade events          |

## What runs inside vs outside the container

| Operation                  | Default (subprocess) | Container tenant           |
| -------------------------- | -------------------- | -------------------------- |
| `bash`, `git`              | host subprocess      | `docker exec -w /work/...` |
| `execute_code` (Python/JS) | host subprocess      | inside container           |
| File reads/writes (fs)     | host                 | host                       |
| Web/tool calls             | host                 | host                       |

Only command execution moves into the container. Filesystem stays on
the host so that IDE-style tools (the web UI, lsp) can keep working
without copying bytes back and forth.

## Configuration

Per-tenant config lives in `<data_dir>/tenants/<tid>/sandbox.toml`:

```toml
# All four dimensions optional; defaults inherited from base image.
image_tag        = "mini_cc-sandbox:latest"
dockerfile_path  = "/abs/path/Dockerfile"      # overrides built-in
apt_packages     = ["ripgrep", "fd-find"]
pip_packages     = ["pandas==2.2", "numpy"]
node_packages    = ["typescript", "tsx"]
extra_mounts     = ["/host/secrets:/secrets:ro"]
```

The manager rebuilds the image on the first `start` after a config
change, then keeps the container warm across sessions.

## CLI

```bash
# Pre-build the image (offline-friendly)
python -m mini_cc.server sandbox build-image --tag mini_cc-sandbox:latest

# Inspect running containers
python -m mini_cc.server sandbox status
python -m mini_cc.server sandbox status --tid acme

# Stop + remove one tenant's container
python -m mini_cc.server sandbox stop acme
```

## Auto-degrade in detail

`probe_docker()` runs once at boot. If the daemon is unreachable
(`docker info` non-zero exit, OS unsupported, etc.), every
container-enabled tenant silently uses `SubprocessSandbox` instead and
a `DegradeEvent(tenant_id, reason)` is appended to
`ServerRuntimeContext.degrades`. The events feed the metrics layer so
operators can alert on "tenant expected container, got subprocess".

Tests inject `docker_available=False` and a `FakeRuntime` to exercise
the degrade path without a real daemon.

## Workspaces + mount path

```
<data_dir>/
  tenants/<tid>/
    projects/<pid>/         # ← host path
      workspace/            # mounted into container at /workspaces/<pid>
      .state/
      meta.json
    .storage/<pid>/
    .mini_cc/
    sandbox.toml
```

The container's `host_projects_dir` is `<data_dir>/tenants/<tid>/projects`
— **not** `<data_dir>/projects`. The earlier mount-path bug bypassed
the tenant layer entirely and was fixed in `cb5f25a`.

## What's still deferred

- Per-call container snapshots / ephemeral containers (we reuse one
  long-running container per tenant — start-up cost amortised).
- gVisor/Kata-style strong isolation. Docker default seccomp profile is
  what runs.

---

# OpenSandbox remote backend (P6)

P6 adds a second container backend: an HTTP-driven
[OpenSandbox](https://github.com/anthropics/opensandbox) lifecycle
server. Where P5's `DockerRuntime` shells out to a local `docker`
daemon, `OpenSandboxRuntime` talks to a remote FastAPI service that
manages a fleet of sandboxes (Docker today, K8s planned).

## When to use which

| Need | Backend |
|---|---|
| Single dev machine, fast startup, no ops | `docker` (P5 default) |
| Shared cluster, multi-tenant fleet, K8s scheduling | `opensandbox` |
| Cross-region distributed runs | `opensandbox` |
| Tight host bind-mounts (workspace on this host's disk) | `docker` |
| Object storage / PVC mounts | `opensandbox` |

Both backends honour the same `ContainerRuntime` Protocol, so the
manager, sandbox layer, and CLI commands work unchanged.

## Selection

Three-tier fallback (first match wins):

1. **Explicit:** `MINI_CC_SANDBOX_BACKEND=opensandbox|docker|auto`
2. **Auto with env config:** if `OPEN_SANDBOX_DOMAIN` or
   `OPEN_SANDBOX_API_KEY` is set, try opensandbox first.
3. **Auto without env:** docker if available, else subprocess.

```bash
# .env
MINI_CC_SANDBOX_BACKEND=auto          # auto-select
OPEN_SANDBOX_DOMAIN=172.28.76.178:11123
OPEN_SANDBOX_API_KEY=sk-opensandbox-...
OPEN_SANDBOX_PROTOCOL=http            # default
```

If opensandbox is requested but unreachable, the server logs a warning
and falls through to docker (so a misconfig doesn't brick the server).

## Mount model

`MountSpec` (Phase 2) is the backend-agnostic mount description:

```python
from mini_cc.sandbox.config import MountSpec, HostMount, PVCMount

# Bind mount (DockerRuntime, OpenSandbox Docker provider)
MountSpec(name="w", mount_path="/workspaces",
         backend=HostMount(path="/host/p"))

# Persistent volume claim (OpenSandbox K8s provider only)
MountSpec(name="data", mount_path="/data",
         backend=PVCMount(claim_name="t1-pvc", storage="5Gi"))

# Read-only with sub-path
MountSpec(name="cfg", mount_path="/etc/app",
         backend=HostMount(path="/host/cfg"),
         read_only=True, sub_path="prod")
```

| Backend | HostMount | PVCMount | OSSFSMount |
|---|---|---|---|
| `DockerRuntime` | ✅ | ❌ (raises ValueError) | ❌ |
| `OpenSandboxRuntime` (Docker) | ✅ | ❌ | ❌ |
| `OpenSandboxRuntime` (K8s) | ⚠️ host-path-in-pod, often wrong | ✅ | ✅ |

When the runtime doesn't support a backend, it fails fast with a clear
error rather than silently dropping the mount.

## Code interpreter (stateful REPL)

The `execute_code` tool (python) gains an optional OpenSandbox backend.
When `MINI_CC_REPL_BACKEND=opensandbox` is set, python execution goes
through execd's `/code/context` + `/code` endpoints — a Jupyter kernel
session stays alive per context, so variables survive across calls.

```bash
MINI_CC_REPL_BACKEND=opensandbox    # opt-in; default is local pickle-wrapper
```

Falls back silently to the local path if the runtime isn't
OpenSandbox-backed or the sandbox isn't running yet.

## Optional MCP template

For ad-hoc sandbox management (create/list/delete outside the agent's
own tenant container), the OpenSandbox project ships an
`opensandbox-mcp` server. mini_cc does **not** auto-provision it —
copy the template into your project to enable:

```bash
cp mini_cc/sandbox/templates/opensandbox.mcp.json \
   .mini_cc/.mcp.json
# Edit env values, then restart the server
```

After restart, `/tools` will show `mcp__opensandbox__sandbox_create`
and friends.

**Trade-off:** the MCP path keeps sandbox state inside the MCP server
process. Restart drops open handles. Use the main HTTP adapter
(above) for per-tenant persistent containers; MCP is for exploratory
or admin tasks.

## What P6 deliberately omits

- **gVisor/Kata/Firecracker**: spec supports `[secure_runtime]` config;
  server-side concern, mini_cc doesn't expose the switch.
- **Snapshot/restore**: spec-complete, but no current use case for
  pausing tenant workspaces.
- **LSP**: no native support in OpenSandbox (FS/exec/lifecycle only).
  Code navigation is already covered by mini_cc's read/edit/grep/glob
  tools. If needed later, run pyright/gopls in-sandbox and expose via
  `sandbox_get_endpoint`.
- **Multi-SDK languages**: Python is mini_cc's only host language.
