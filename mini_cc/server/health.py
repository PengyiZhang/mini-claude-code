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

# Module-level caches: production runs ONE app per process, so global
# state is fine. Tests build many apps (different data_dirs) and reset
# both dicts via their autouse fixture — see tests/test_m4_readyz.py.
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
