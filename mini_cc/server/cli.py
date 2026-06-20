"""CLI entrypoints: ``python -m mini_cc.server`` and key management.

Subcommands:
  serve                       Run the HTTP/SSE server (default if no args)
  keygen <tenant_id>          Create a new API key for a tenant
  keys <tenant_id>            List all active keys for a tenant
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


def cmd_serve(args) -> int:
    import uvicorn
    from ..projects import ProjectManager
    from ..session import SessionManager
    from .app import build_app

    data_dir = _data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)

    pm = ProjectManager(data_dir / "projects")
    sm = SessionManager(pm)
    reg = _key_registry()

    cors_raw = _env("MINI_CC_CORS_ORIGINS", "")
    cors_origins = [o.strip() for o in cors_raw.split(",") if o.strip()]

    app = build_app(data_dir=data_dir, key_registry=reg, pm=pm, sm=sm,
                    cors_origins=cors_origins)

    host = _env("MINI_CC_HOST", "127.0.0.1")
    port = int(_env("MINI_CC_PORT", "8000"))
    print(f"[mini_cc] data_dir={data_dir} host={host} port={port}")
    uvicorn.run(app, host=host, port=port, handle_signals=True)
    return 0


def cmd_keygen(args) -> int:
    reg = _key_registry()
    key = reg.generate(args.tenant_id)
    print(key)
    return 0


def cmd_keys(args) -> int:
    reg = _key_registry()
    for k in reg.list_for(args.tenant_id):
        print(k)
    return 0


def cmd_revoke(args) -> int:
    reg = _key_registry()
    if reg.revoke(args.key):
        print("revoked")
        return 0
    print("not found", file=sys.stderr)
    return 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="mini_cc.server")
    sub = p.add_subparsers(dest="cmd")

    p_serve = sub.add_parser("serve", help="Run the HTTP server")
    p_serve.set_defaults(func=cmd_serve)

    p_keygen = sub.add_parser("keygen", help="Create an API key")
    p_keygen.add_argument("tenant_id")
    p_keygen.set_defaults(func=cmd_keygen)

    p_keys = sub.add_parser("keys", help="List keys for a tenant")
    p_keys.add_argument("tenant_id")
    p_keys.set_defaults(func=cmd_keys)

    p_revoke = sub.add_parser("revoke", help="Revoke a key")
    p_revoke.add_argument("key")
    p_revoke.set_defaults(func=cmd_revoke)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        # No subcommand → serve
        return cmd_serve(args)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
