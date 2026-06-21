# Phase G: configurable container-level sandbox isolation (multi-tenant)

## Context

Phases A–F are shipped (397 backend tests). The existing `SubprocessSandbox`
locks every tool operation behind a per-project root directory and an
env whitelist, plus a command-policy scanner. That model is
defense-in-depth **at the host-process layer** — but a clever shell
escape (symlink tricks, `LD_PRELOAD`, kernel exploits) still reaches host
resources. Phase G adds a **second** isolation layer: every tool
`execute()`/`git()` call runs inside a per-tenant Docker container, so
even a successful escape from the project workspace only reaches that
tenant's container (and the bind-mounted tenant workspace), not the
host or other tenants.

Threat model: **(c) both layers** — per-tenant containers stop
cross-tenant leakage even if the host-side path isolation fails; the
in-container project-root restriction (already enforced by
`SubprocessSandbox` on the bind-mounted fs) stops cross-project
leakage inside one tenant.

Backward compat: default behaviour unchanged. If neither
`MINI_CC_SANDBOX_DEFAULT=container` nor any `tenants/{tid}/sandbox.toml`
with `enabled = true` is present, every tenant keeps using
`SubprocessSandbox` exactly as before.

## Approach (locked via brainstorm)

- **Runtime**: Docker Engine CLI (no SDK dep). Dev = Docker Desktop on
  Windows/WSL2; prod = Linux + Docker Engine.
- **Threat model**: per-tenant containers **and** in-container project
  root restriction.
- **Lifecycle**: long-running per-tenant container (`sleep infinity`),
  `docker exec` per tool call. Lazy start on first `execute()`/`git()`.
- **Workspace storage**: bind-mount the tenant's whole projects dir →
  `/workspaces/` inside the container. Each project is `/workspaces/{pid}/`.
- **Image**: repo-shipped `mini_cc/sandbox/Dockerfile` →
  `mini_cc-sandbox:latest` (python:3.12-slim + git + ripgrep + node 20 +
  build-essential). No venv injection — agents create their own venvs
  the same way a developer would on a fresh VM.
- **Network**: default `--network=none`; per-tenant `network = "bridge"`
  opt-in for tenants that need `npm install` etc.
- **Config**: two-layer. Server env `MINI_CC_SANDBOX_DEFAULT=subprocess|container`
  (default `subprocess`) sets global; `tenants/{tid}/sandbox.toml`
  with `enabled = true` overrides per tenant.
- **Sandbox split**: only `execute()`/`git()` go through the container.
  File ops (`read`/`write`/`edit`/`glob`/`grep`) stay on host —
  tenant isolation already enforced at `ProjectManager` path layer;
  containerizing fs would add ~30ms × dozens of reads per turn.
- **Failure mode**: fail-fast. If `enabled=true` but Docker is missing,
  the project-open call raises (rendered 503), **not** silent fallback
  to subprocess (silent fallback would defeat the entire point).

## File-by-file

### `mini_cc/sandbox/runtime.py` (new, ~120 lines)

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
    """All docker calls go through subprocess; no SDK dep."""
    def is_available(self) -> bool: ...
    def ensure_running(self, *, name, image, mount, network, ...):
        # status check → if "running", return; if "exited", docker start;
        # if "missing", docker run -d --restart=unless-stopped ...
    def exec(self, *, name, workdir, command, timeout, env):
        # docker exec -w {workdir} [-e K=V ...] {name} sh -c "{cmd}"
        # (or {name} {cmd...} for list-form commands like git)
    def status(self, name) -> str: ...
```

A `FakeRuntime` test double lives in `tests/test_phaseG_sandbox_container.py`
and records every call into `.calls: list[tuple]`. **No test depends
on a real Docker daemon.**

### `mini_cc/sandbox/config.py` (new, ~70 lines)

```python
@dataclass
class ContainerConfig:
    enabled: bool = False
    network: str = "none"              # "none" | "bridge"
    image: str = "mini_cc-sandbox:latest"
    cpu_quota: str | None = None       # "1.0"
    memory_limit: str | None = None    # "512m"

def server_default_kind() -> str:
    """MINI_CC_SANDBOX_DEFAULT=subprocess|container (default subprocess)."""

def load_tenant_config(tid: str, tenants_dir: Path) -> ContainerConfig | None:
    """Read tenants/{tid}/sandbox.toml. None if file missing."""

def resolve_kind(tid, tenants_dir) -> tuple[str, ContainerConfig | None]:
    """Returns ('subprocess', None) or ('container', cfg)."""
```

`sandbox.toml` is **cold-loaded**: cached in
`ProjectManager._sandbox_configs[tid]` on first access, file edits
require a server restart. Hot-reload would mean recreating containers,
violating the long-running-container assumption.

### `mini_cc/sandbox/manager.py` (new, ~80 lines)

```python
class TenantContainerManager:
    """Owns the per-tenant container lifecycle. One instance per tid."""

    def __init__(self, tid: str, host_projects_dir: Path,
                 config: ContainerConfig, runtime: ContainerRuntime):
        self.tid = tid
        self.container_name = _container_name(tid)  # sanitized
        self.host_projects_dir = host_projects_dir
        self.config = config
        self.runtime = runtime

    def ensure_running(self) -> None:
        """Idempotent: status check → run/start as needed."""

    def exec(self, *, workdir, command, timeout, env):
        self.ensure_running()
        return self.runtime.exec(name=self.container_name, workdir=workdir,
                                  command=command, timeout=timeout, env=env)

    def stop(self) -> None: ...
```

`_container_name(tid)` sanitizes tid to `[A-Za-z0-9][A-Za-z0-9_.-]*`
and prefixes `mini_cc-`, truncated to 63 chars. Server lifespan uses
`list_managed()` to find every `mini_cc-*` container, so a restarted
server cleans up containers left over from a crashed previous run.

### `mini_cc/sandbox/container.py` (new, ~70 lines)

```python
class ContainerSandbox:
    """Sandbox protocol impl: fs ops via host, exec/git via container."""

    def __init__(self, project_id, project_root, policy,
                 container_mgr: TenantContainerManager):
        self.project_id = project_id
        self.project_root = project_root
        self.policy = policy
        self._fs = SubprocessSandbox(project_id, project_root, policy)
        self._mgr = container_mgr

    # fs: delegate to embedded SubprocessSandbox
    def resolve_path(self, rel): return self._fs.resolve_path(rel)
    def validate_path(self, p):  return self._fs.validate_path(p)
    def read(self, *a, **kw):    return self._fs.read(*a, **kw)
    def write(self, *a, **kw):   return self._fs.write(*a, **kw)
    def edit(self, *a, **kw):    return self._fs.edit(*a, **kw)
    def glob(self, *a, **kw):    return self._fs.glob(*a, **kw)
    def grep(self, *a, **kw):    return self._fs.grep(*a, **kw)

    # exec/git: through container
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
        # reuse SubprocessSandbox's env whitelist
        env = {k: v for k, v in os.environ.items()
               if k in self.policy.allowed_env}
        env["HOME"] = f"/workspaces/{self.project_id}"
        if extra: env.update(extra)
        return env
```

The host-side `_fs` SubprocessSandbox is what guarantees fs paths stay
inside `project_root`. The container's `workdir=/workspaces/{pid}`
makes the in-container cwd match. `HOME` is set to the in-container
project dir so tools that look at `$HOME` (git config, npm cache) land
inside the workspace.

### `mini_cc/sandbox/Dockerfile` (new, ~30 lines)

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

No venv, no project deps. Agents that want isolated Python deps run
`!python -m venv .venv && .venv/bin/pip install ...` themselves — same
workflow as a developer on a fresh VM.

### `mini_cc/projects/manager.py` (modify, +20 lines)

```python
class ProjectManager:
    def __init__(self, root, *, metrics=None,
                 sandbox_factory: Callable[[str, str, Path, Policy], Sandbox]
                     | None = None):
        ...
        self._sandbox_factory = sandbox_factory or _default_sandbox_factory

    def get(self, pid) -> Project:
        # existing logic, but sandbox = self._sandbox_factory(tid, pid, ws, policy)
```

Default factory returns `SubprocessSandbox`. When `cmd_serve` is
building the server, it constructs a factory closure that calls
`resolve_kind(tid, tenants_dir)` and returns either
`SubprocessSandbox` or `ContainerSandbox`.

### `mini_cc/server/app.py` (modify, +15 lines)

```python
@asynccontextmanager
async def lifespan(app):
    yield
    # Stop sessions, then containers
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

`build_app` gains a `container_runtime: ContainerRuntime | None = None`
kwarg stored on `app.state`.

### `mini_cc/server/cli.py` (modify, +60 lines)

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

New `sandbox` subcommand:

- `python -m mini_cc.server sandbox build-image [--tag ...] [--no-cache]`
- `python -m mini_cc.server sandbox status [--tid TID]`
- `python -m mini_cc.server sandbox stop TID`

### `tests/test_phaseG_sandbox_config.py` (new)

- `MINI_CC_SANDBOX_DEFAULT=container` makes absent tenant config resolve to container.
- `tenants/{tid}/sandbox.toml enabled=true` overrides server default.
- `enabled=false` falls back to server default.
- Invalid `network` value → ValueError.
- `cpu_quota`/`memory_limit` pass-through.

### `tests/test_phaseG_sandbox_container.py` (new)

`FakeRuntime` records every call. Tests:

- First `execute()` triggers `ensure_running()` (one `run` call).
- Subsequent `execute()` skips `run`.
- `status="running"` short-circuits `ensure_running`.
- `status="exited"` triggers `docker start`, not `docker run`.
- `write()`/`read()`/`glob()`/`grep()` make **zero** runtime calls.
- Policy violation raises before any runtime call.
- `DockerMissingError` raised when `is_available()` returns False.
- Container workdir is `/workspaces/{pid}` for both execute and git.
- env whitelist filters host env; `HOME` set to in-container project dir.
- `_container_name(tid)` sanitizes special chars and respects 63-char limit.

### `tests/test_phaseG_sandbox_http.py` (new)

End-to-end via `TestClient(build_app(..., container_runtime=fake_rt,
sandbox_factory=...))`:

- Tenant without `sandbox.toml` and default server env → fake_rt.calls is empty.
- Tenant with `enabled=true` → driving a bash tool emits an `exec` call.
- Container exec timeout → returns `returncode=124` (no exception).
- Tenant with `enabled=true` but `fake_rt.is_available()=False` →
  project-open returns 503 with `sandbox_unavailable` code.

### `mini_cc/README.md` + `.zh.md` (modify)

New "Container sandbox (Phase G)" section covering:

- Threat model summary.
- `MINI_CC_SANDBOX_DEFAULT` env var.
- `tenants/{tid}/sandbox.toml` schema + example.
- `sandbox build-image` / `status` / `stop` CLI.
- Windows + Docker Desktop file-sharing note.
- Manual verification checklist (build image, drive chat turn,
  verify `!ls /` shows `/workspaces/...`, verify `!curl` fails on
  `--network=none`, toggle `network = "bridge"`, restart server).

## Verification

```bash
# 1. Unit tests (no docker daemon needed)
python -m pytest tests/test_phaseG_sandbox_config.py -v
python -m pytest tests/test_phaseG_sandbox_container.py -v
python -m pytest tests/test_phaseG_sandbox_http.py -v

# 2. Full suite (expect ~430 passing)
python -m pytest tests/ -q

# 3. Build image
python -m mini_cc.server sandbox build-image
docker images mini_cc-sandbox

# 4. Manual end-to-end (requires docker daemon)
mkdir -p $MINI_CC_DATA_DIR/tenants/t1
echo 'enabled = true' > $MINI_CC_DATA_DIR/tenants/t1/sandbox.toml
python -m mini_cc.server &
# In chat: !ls /  → should show /workspaces/...
# In chat: !curl http://example.com  → should fail
# Edit sandbox.toml to set network = "bridge", restart server, retry curl
docker ps | grep mini_cc-t1

# 5. Container cleanup on shutdown
python -m mini_cc.server sandbox status   # should be empty after server stop
```

## Out of scope for Phase G

- **Per-project sandbox config override.** Sandbox is a tenant-level
  concern; project-level permissions stay in `permissions.toml`.
- **Hot-reload of sandbox.toml.** Changes require server restart.
- **Auto image build.** First run with `enabled=true` and missing image
  fails loud (`image_missing`) rather than auto-building. Build is
  network-bound and slow; should never block an HTTP request.
- **Podman / gVisor / Firecracker backend.** `ContainerRuntime` is
  abstracted; only Docker is implemented.
- **Multi-arch images.** Dockerfile builds for current arch only; arm64
  users run `docker buildx build` themselves.
- **Windows native containers.** Linux containers only (WSL2 backend).
- **Rootless Docker / userns-remap.** Tested with standard Docker
  Engine + Docker Desktop only.
- **`docker stats` metrics collection.** `cpu_quota`/`memory_limit`
  are hard caps but no per-container usage is surfaced to the metrics
  registry. Future work.
- **Python venv injection.** Agents that want isolated Python deps
  create their own venv. The container image ships only system
  Python + node + git.
- **Task queue / job-based async turns.** The HTTP `/send` endpoint
  remains streaming-first. Decoupling `loop.run()` from the HTTP
  handler via `POST /send?mode=job` + `GET /jobs/{job_id}/events`
  is tracked separately as **Phase H** (see Future Work).
