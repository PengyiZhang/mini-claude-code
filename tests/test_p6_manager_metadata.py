"""TenantContainerManager now passes tid (the logical identity) to the
runtime, not a pre-sanitized container name. This lets each runtime
decide how to use it: DockerRuntime synthesizes ``mini_cc-<sanitized>``
for the docker CLI; OpenSandboxRuntime indexes by metadata ``mini-cc-tid``.

The contract:
- runtime.ensure_running(name=tid, ...)
- runtime.exec(name=tid, ...)
- runtime.status(name=tid)
- runtime.stop(name=tid)
- runtime.remove(name=tid)

``container_name`` stays on the manager for human-readable logs/CLI."""
from __future__ import annotations

from pathlib import Path

from mini_cc.sandbox.config import ContainerConfig
from mini_cc.sandbox.manager import TenantContainerManager
from mini_cc.sandbox.runtime import FakeRuntime


def _build_mgr(tid: str = "t1") -> tuple[TenantContainerManager, FakeRuntime]:
    rt = FakeRuntime()
    mgr = TenantContainerManager(
        tid=tid,
        host_projects_dir=Path("/data/tenants/t1/projects"),
        config=ContainerConfig(enabled=True, image_tag="img:v1", network="none"),
        runtime=rt)
    return mgr, rt


def test_ensure_running_passes_tid_as_name():
    """runtime.ensure_running gets the raw tid, not mini_cc-<sanitized>."""
    mgr, rt = _build_mgr(tid="my-tid-1")
    mgr.ensure_running()
    _, kwargs = rt.calls[0]
    assert kwargs["name"] == "my-tid-1"


def test_exec_passes_tid_as_name():
    mgr, rt = _build_mgr()
    mgr.exec(project_id="p1", command="ls", timeout=5, env={})
    exec_kwargs = next(c[1] for c in rt.calls if c[0] == "exec")
    assert exec_kwargs["name"] == "t1"


def test_stop_passes_tid_as_name():
    mgr, rt = _build_mgr()
    mgr.stop()
    assert any(c[0] == "stop" and c[1]["name"] == "t1" for c in rt.calls)


def test_container_name_attribute_preserved_for_display():
    """container_name stays on the manager for logs/CLI display only —
    callers that show it to humans keep working."""
    mgr, _ = _build_mgr(tid="acme.corp")
    assert mgr.container_name == "mini_cc-acme.corp"