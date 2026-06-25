"""CLI entrypoints: ``python -m mini_cc.server`` and key management.

Subcommands:
  serve                       Run the HTTP/SSE server (default if no args)
  keygen <tenant_id>          Create a new API key for a tenant
  keys list <tenant_id>       List all keys (incl. expired) for a tenant
  keys rotate <key>           Rotate a key (with optional --grace-hours)
  revoke <key>                Revoke a key
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _data_dir() -> Path:
    return Path(_env("MINI_CC_DATA_DIR", "./mini_cc_data"))


def _key_registry():
    from ..auth import TenantKeyRegistry
    return TenantKeyRegistry(_data_dir() / "keys.json")


def _parse_rate_limit_env() -> tuple[int, dict[str, int]]:
    """Parse MINI_CC_RATE_LIMIT_RPM_DEFAULT and MINI_CC_RATE_LIMIT_RPM.

    The latter is ``tenant=rpm,tenant=rpm,...``. Returns (default, overrides).
    """
    default = int(_env("MINI_CC_RATE_LIMIT_RPM_DEFAULT", "60"))
    overrides: dict[str, int] = {}
    raw = _env("MINI_CC_RATE_LIMIT_RPM", "")
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "=" not in chunk:
            raise ValueError(
                f"MINI_CC_RATE_LIMIT_RPM entry {chunk!r} must be tenant=rpm")
        t, r = chunk.split("=", 1)
        t = t.strip()
        r = r.strip()
        if not t or not r.isdigit():
            raise ValueError(
                f"MINI_CC_RATE_LIMIT_RPM entry {chunk!r} malformed")
        overrides[t] = int(r)
    return default, overrides


def cmd_serve(args) -> int:
    import uvicorn
    import logging
    from ..projects import ProjectManager
    from ..sandbox import probe_docker
    from ..session import SessionManager
    from .app import build_app
    from .logging_config import configure_logging
    from .metrics import default_registry
    from .ratelimit import TenantRateLimiter
    from .runtime_context import ServerRuntimeContext

    configure_logging(
        format=_env("MINI_CC_LOG_FORMAT", "json"),
        level=_env("MINI_CC_LOG_LEVEL", "INFO"),
    )

    data_dir = _data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)

    metrics = default_registry()

    avail = probe_docker()
    if not avail.available:
        # Don't fail — log and let ServerRuntimeContext auto-degrade per tenant.
        # Honours user requirement #2: container env unsupported → fall back.
        logging.getLogger("mini_cc").warning(
            "docker unavailable (%s); container-enabled tenants will "
            "auto-degrade to subprocess sandbox", avail.reason)

    # Materialize the system-tier plugin dir so operators can drop global
    # skills + mcp.toml in <data_dir>/.mini_cc/ without manual setup.
    # Idempotent; cheap.
    from ..plugins import PluginTier, ensure_tier_dir
    ensure_tier_dir(data_dir, PluginTier.SYSTEM)

    ctx = ServerRuntimeContext(
        data_dir=data_dir,
        key_registry=_key_registry(),
        docker_available=avail.available,
    )
    # ServerRuntimeContext may have selected a non-docker backend (e.g.
    # OpenSandbox) via MINI_CC_SANDBOX_BACKEND. If so, treat that as
    # "container backend available" even though probe_docker returned
    # unavailable — the auto-degrade path checks this flag.
    backend_available = avail.available or (ctx._runtime is not None)
    ctx.docker_available = backend_available

    pm = ctx.build_project_manager()
    sm = SessionManager(pm)
    reg = ctx.key_registry

    default_rpm, overrides = _parse_rate_limit_env()
    limiter = TenantRateLimiter(default_rpm=default_rpm, overrides=overrides)

    cors_raw = _env("MINI_CC_CORS_ORIGINS", "*")
    cors_origins = [o.strip() for o in cors_raw.split(",") if o.strip()]

    app = build_app(data_dir=data_dir, key_registry=reg, pm=pm, sm=sm,
                    cors_origins=cors_origins, rate_limiter=limiter,
                    metrics_registry=metrics, server_runtime=ctx)

    host = _env("MINI_CC_HOST", "127.0.0.1")
    port = int(_env("MINI_CC_PORT", "8000"))

    backend_name = type(ctx._runtime).__name__ if ctx._runtime else "none"
    logging.getLogger("mini_cc").info(
        "starting server",
        extra={"host": host, "port": port,
               "rate_limit_default_rpm": default_rpm,
               "rate_limit_overrides": overrides,
               "data_dir": str(data_dir),
               "docker_available": backend_available,
               "sandbox_backend": backend_name})
    uvicorn.run(app, host=host, port=port)
    return 0


def _format_record(rec) -> str:
    """One-line human summary of a KeyRecord."""
    scopes = ",".join(rec.scopes) if rec.scopes else "-"
    exp = rec.expires_at or "never"
    label = f" [{rec.label}]" if rec.label else ""
    rot = f" (rotated from {rec.rotated_from[:12]}…)" if rec.rotated_from else ""
    return f"{rec.key}  tenant={rec.tenant_id}  scopes={scopes}  expires={exp}{label}{rot}"


def cmd_keygen(args) -> int:
    reg = _key_registry()
    try:
        rec = reg.generate(
            args.tenant_id,
            scopes=args.scopes or None,
            expires_in=args.expires_in,
            label=args.label or "",
        )
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(_format_record(rec))
    return 0


def cmd_keys_list(args) -> int:
    reg = _key_registry()
    recs = reg.list_for(args.tenant_id)
    if not recs:
        return 0
    for rec in sorted(recs, key=lambda r: r.created_at):
        print(_format_record(rec))
    return 0


def cmd_keys_rotate(args) -> int:
    reg = _key_registry()
    try:
        new_rec, old_rec = reg.rotate(
            args.key,
            grace_hours=args.grace_hours,
            scopes=args.scopes,
            expires_in=args.expires_in,
            label=args.label or "",
        )
    except KeyError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print("new: " + _format_record(new_rec))
    if old_rec is not None:
        print("old: " + _format_record(old_rec))
    else:
        print("old: (hard-revoked)")
    return 0


def cmd_revoke(args) -> int:
    reg = _key_registry()
    if reg.revoke(args.key):
        print("revoked")
        return 0
    print("not found", file=sys.stderr)
    return 1


def cmd_sandbox_build_image(args) -> int:
    """python -m mini_cc.server sandbox build-image [--tag T] [--dockerfile P]"""
    from ..sandbox.osdetect import probe_docker
    from ..sandbox.imagebuild import build_image
    from ..sandbox.config import ContainerConfig
    cfg = ContainerConfig(
        image_tag=args.tag,
        dockerfile_path=args.dockerfile,
    )
    # Probe the argv_prefix so ``wsl docker build`` is used when Docker
    # lives inside a WSL2 distro on Windows.
    prefix = probe_docker(force=True).argv_prefix
    try:
        build_image(args.tag, cfg, prefix=prefix)
    except Exception as exc:
        print(f"build failed: {exc}", file=sys.stderr)
        return 1
    print(f"image built: {args.tag}")
    return 0


def _get_sandbox_runtime():
    """Pick a runtime instance for ``sandbox status/stop`` CLI commands.

    Mirrors ServerRuntimeContext._build_runtime's three-tier fallback:
    opensandbox (if env configured) → docker → None. The CLI uses this
    instead of hardcoding DockerRuntime so it stays consistent with the
    running server's backend choice."""
    import os
    if os.environ.get("OPEN_SANDBOX_DOMAIN") \
            or os.environ.get("OPEN_SANDBOX_API_KEY"):
        try:
            from ..sandbox.opensandbox_runtime import (
                OpenSandboxConfig, OpenSandboxRuntime)
            rt = OpenSandboxRuntime(OpenSandboxConfig.from_env())
            if rt.is_available():
                return rt
        except Exception:
            pass
    from ..sandbox.osdetect import probe_docker
    from ..sandbox.runtime import DockerRuntime
    return DockerRuntime(prefix=probe_docker(force=True).argv_prefix)


def cmd_sandbox_status(args) -> int:
    """python -m mini_cc.server sandbox status [--tid TID]"""
    rt = _get_sandbox_runtime()
    if not rt.is_available():
        print("no container backend available", file=sys.stderr)
        return 1
    names = rt.list_managed()
    if args.tenant_id:
        # OpenSandboxRuntime.list_managed returns tids directly;
        # DockerRuntime returns container names like ``mini_cc-<tid>``.
        # Filter loosely so both shapes work.
        wanted_tid = args.tenant_id
        from ..sandbox.manager import _container_name
        wanted_name = _container_name(args.tenant_id)
        names = [n for n in names if n == wanted_tid or n == wanted_name]
    for n in names:
        st = rt.status(n)
        print(f"{n}\t{st}")
    return 0


def cmd_sandbox_stop(args) -> int:
    """python -m mini_cc.server sandbox stop TID"""
    from ..sandbox.manager import _container_name
    rt = _get_sandbox_runtime()
    # OpenSandboxRuntime takes tid directly; DockerRuntime wants the
    # ``mini_cc-<tid>`` container name. Pass both and let the runtime
    # decide (status/stop are idempotent and tolerant of unknown names).
    candidates = [args.tenant_id, _container_name(args.tenant_id)]
    try:
        for name in candidates:
            rt.stop(name)
            rt.remove(name)
    except Exception as exc:
        print(f"stop failed: {exc}", file=sys.stderr)
        return 1
    print(f"stopped: {name}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="mini_cc.server")
    sub = p.add_subparsers(dest="cmd")

    p_serve = sub.add_parser("serve", help="Run the HTTP server")
    p_serve.set_defaults(func=cmd_serve)

    p_keygen = sub.add_parser("keygen", help="Create an API key")
    p_keygen.add_argument("tenant_id")
    p_keygen.add_argument("--scopes", action="append", default=None,
                          help="Scope to grant (repeat for multiple). "
                               "Default grants ['*']. Examples: 'read:*', "
                               "'sessions:write'.")
    p_keygen.add_argument("--expires-in", dest="expires_in", default=None,
                          help="Lifetime, e.g. '7d', '12h', '3600s', or '3600'. "
                               "Omit for no expiry.")
    p_keygen.add_argument("--label", default=None,
                          help="Free-form label (CI, prod-runner, ...).")
    p_keygen.set_defaults(func=cmd_keygen)

    p_keys = sub.add_parser("keys", help="Key management")
    keys_sub = p_keys.add_subparsers(dest="keys_cmd", required=True)

    p_keys_list = keys_sub.add_parser("list", help="List keys for a tenant")
    p_keys_list.add_argument("tenant_id")
    p_keys_list.set_defaults(func=cmd_keys_list)

    p_keys_rotate = keys_sub.add_parser("rotate", help="Rotate a key")
    p_keys_rotate.add_argument("key")
    p_keys_rotate.add_argument("--grace-hours", dest="grace_hours",
                               type=int, default=0,
                               help="Old key keeps working for this many "
                                    "hours. Default 0 = hard revoke.")
    p_keys_rotate.add_argument("--scopes", action="append", default=None,
                               help="Override scopes on the new key. "
                                    "Default inherits the old key's scopes.")
    p_keys_rotate.add_argument("--expires-in", dest="expires_in", default=None,
                               help="Lifetime for the new key (e.g. '30d').")
    p_keys_rotate.add_argument("--label", default=None,
                               help="Override label on the new key.")
    p_keys_rotate.set_defaults(func=cmd_keys_rotate)

    p_revoke = sub.add_parser("revoke", help="Revoke a key")
    p_revoke.add_argument("key")
    p_revoke.set_defaults(func=cmd_revoke)

    p_sandbox = sub.add_parser("sandbox", help="Container sandbox management")
    sandbox_sub = p_sandbox.add_subparsers(dest="sandbox_cmd", required=True)

    p_sb_build = sandbox_sub.add_parser("build-image",
        help="Build the mini_cc sandbox Docker image")
    p_sb_build.add_argument("--tag", default="mini_cc-sandbox:latest",
        help="Image tag (default: mini_cc-sandbox:latest)")
    p_sb_build.add_argument("--dockerfile", default=None,
        help="Custom Dockerfile path. If omitted, uses the package default "
             "plus any declarative packages from --tid's sandbox.toml.")
    p_sb_build.set_defaults(func=cmd_sandbox_build_image)

    p_sb_status = sandbox_sub.add_parser("status",
        help="List managed containers and their status")
    p_sb_status.add_argument("--tid", dest="tenant_id", default=None,
        help="Filter to one tenant's container.")
    p_sb_status.set_defaults(func=cmd_sandbox_status)

    p_sb_stop = sandbox_sub.add_parser("stop",
        help="Stop and remove a tenant's container")
    p_sb_stop.add_argument("tenant_id")
    p_sb_stop.set_defaults(func=cmd_sandbox_stop)

    return p


def main(argv: list[str] | None = None) -> int:
    # Load .env *before* any subcommand reads os.environ. Without this,
    # users had to remember to `export $(cat .env | xargs)` (or worse,
    # paste every var inline) — even though python-dotenv was already
    # declared as a dependency. We search the standard chain: cwd,
    # then the directory containing this file (repo root).
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except Exception:
        # dotenv is in our deps, but don't let a missing optional dep
        # brick the whole CLI.
        pass

    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        # No subcommand → serve
        return cmd_serve(args)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
