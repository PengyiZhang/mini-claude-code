"""MountSpec: backend-agnostic mount description.

MountSpec decouples the manager (which knows what to mount) from each
runtime (which knows how to translate to docker -v / OpenSandbox
Volume / k8s PVC). Phase 2 only adds host + PVC; OSSFS is a placeholder
for Phase 3+ so the discriminated union is total from day one."""
from __future__ import annotations

import pytest

from mini_cc.sandbox.config import HostMount, MountSpec, OSSFSMount, PVCMount


# ── direct construction ────────────────────────────────────────────────────

def test_host_mount_round_trip():
    m = MountSpec(name="w", mount_path="/workspaces",
                  backend=HostMount(path="/host/p"))
    assert m.backend.path == "/host/p"
    assert m.read_only is False
    assert m.sub_path is None


def test_pvc_mount_defaults():
    m = MountSpec(name="w", mount_path="/workspaces",
                  backend=PVCMount(claim_name="t1-pvc"))
    assert m.backend.create_if_not_exists is True
    assert m.backend.storage_class is None
    assert m.backend.storage is None


def test_pvc_mount_overrides():
    m = MountSpec(name="w", mount_path="/workspaces",
                  backend=PVCMount(claim_name="t1-pvc",
                                   storage_class="fast-ssd",
                                   storage="5Gi",
                                   create_if_not_exists=False))
    assert m.backend.storage_class == "fast-ssd"
    assert m.backend.storage == "5Gi"
    assert m.backend.create_if_not_exists is False


def test_mount_spec_read_only_and_subpath():
    m = MountSpec(name="w", mount_path="/cfg",
                  backend=HostMount(path="/host/cfg"),
                  read_only=True, sub_path="subdir")
    assert m.read_only is True
    assert m.sub_path == "subdir"


# ── legacy tuple compat ───────────────────────────────────────────────────

def test_legacy_tuple_basic():
    m = MountSpec.from_legacy_tuple(("/host/p", "/workspaces", ""))
    assert m.mount_path == "/workspaces"
    assert isinstance(m.backend, HostMount)
    assert m.backend.path == "/host/p"
    assert m.name.startswith("mnt")


def test_legacy_tuple_options_ignored():
    """Options ('ro', 'z', etc.) are docker-specific and don't map cleanly
    onto MountSpec — caller should set .read_only explicitly. We accept
    the tuple but drop the options field rather than guess."""
    m = MountSpec.from_legacy_tuple(("/h", "/c", "ro,Z"))
    assert m.backend.path == "/h"
    assert m.mount_path == "/c"
    # read_only NOT auto-set from options string — caller's responsibility


def test_legacy_tuple_name_sanitized():
    """Host path slashes/backslashes → '_'; leading/trailing stripped."""
    m = MountSpec.from_legacy_tuple(("C:/some/host/path", "/c", ""))
    assert "/" not in m.name
    assert "\\" not in m.name
    assert m.name.startswith("mnt")


def test_legacy_tuple_empty_host_name():
    """Path that's all separators → still yields a non-empty name."""
    m = MountSpec.from_legacy_tuple(("///", "/c", ""))
    assert m.name  # not empty


# ── immutability ─────────────────────────────────────────────────────────

def test_mount_spec_is_frozen():
    m = MountSpec(name="w", mount_path="/c", backend=HostMount(path="/h"))
    with pytest.raises(Exception):
        m.name = "x"  # type: ignore[misc]


# ── DockerRuntime consumes MountSpec ──────────────────────────────────────

def _smart_run(captured: list, initial_status: str = "missing"):
    """Build a subprocess.run stub that handles inspect/start/run.

    - inspect → returns initial_status (or 'running' once we've started)
    - start   → ok, marks running
    - run     → records argv, marks running
    """
    import subprocess
    states: dict[str, str] = {}

    def smart_run(args, **kw):
        captured.append(list(args))
        if args[:2] == ["docker", "inspect"]:
            name = args[-1]
            st = states.get(name, initial_status)
            return subprocess.CompletedProcess(
                args=args, returncode=0, stdout=st + "\n", stderr="")
        if args[:2] == ["docker", "start"]:
            states[args[-1]] = "running"
            return subprocess.CompletedProcess(
                args=args, returncode=0, stdout="", stderr="")
        if args[:2] == ["docker", "run"]:
            states[args[-1]] = "running"
            return subprocess.CompletedProcess(
                args=args, returncode=0, stdout="", stderr="")
        return subprocess.CompletedProcess(
            args=args, returncode=0, stdout="", stderr="")
    return smart_run


def test_docker_runtime_accepts_mount_spec(monkeypatch):
    """DockerRuntime.ensure_running accepts MountSpec (host path bind)."""
    import subprocess
    from mini_cc.sandbox.runtime import DockerRuntime
    captured: list[list[str]] = []
    monkeypatch.setattr(subprocess, "run", _smart_run(captured))
    rt = DockerRuntime()
    rt.ensure_running(
        name="t1", image="img",
        mounts=[MountSpec(name="w", mount_path="/workspaces",
                          backend=HostMount(path="/host"))],
        network="none")
    run_calls = [c for c in captured if c[:2] == ["docker", "run"]]
    assert run_calls, "docker run was never invoked"
    assert any("/host:/workspaces" in part for part in run_calls[0]), \
        f"mount spec not in argv: {run_calls[0]}"


def test_docker_runtime_accepts_legacy_tuple(monkeypatch):
    """Backwards compat: tuple (host, container, options) still works."""
    import subprocess
    from mini_cc.sandbox.runtime import DockerRuntime
    captured: list[list[str]] = []
    monkeypatch.setattr(subprocess, "run", _smart_run(captured))
    rt = DockerRuntime()
    rt.ensure_running(
        name="t1", image="img",
        mounts=[("/host", "/workspaces", "ro")],
        network="none")
    run_calls = [c for c in captured if c[:2] == ["docker", "run"]]
    assert any("/host:/workspaces:ro" in part for part in run_calls[0])


def test_docker_runtime_rejects_pvc(monkeypatch):
    """Docker has no PVC concept — refuse rather than silently drop."""
    import subprocess
    from mini_cc.sandbox.runtime import DockerRuntime
    captured: list[list[str]] = []
    monkeypatch.setattr(subprocess, "run", _smart_run(captured))
    rt = DockerRuntime()
    with pytest.raises(ValueError, match="HostMount"):
        rt.ensure_running(
            name="t1", image="img",
            mounts=[MountSpec(name="w", mount_path="/pvc",
                              backend=PVCMount(claim_name="c1"))],
            network="none")


def test_docker_runtime_mount_spec_read_only(monkeypatch):
    """read_only=True appends :ro to the bind spec."""
    import subprocess
    from mini_cc.sandbox.runtime import DockerRuntime
    captured: list[list[str]] = []
    monkeypatch.setattr(subprocess, "run", _smart_run(captured))
    rt = DockerRuntime()
    rt.ensure_running(
        name="t1", image="img",
        mounts=[MountSpec(name="w", mount_path="/cfg",
                          backend=HostMount(path="/host/cfg"),
                          read_only=True)],
        network="none")
    run_calls = [c for c in captured if c[:2] == ["docker", "run"]]
    assert any("/host/cfg:/cfg:ro" in part for part in run_calls[0])


# ── OpenSandboxRuntime translates MountSpec ───────────────────────────────

def test_opensandbox_translates_host_mount():
    from mini_cc.sandbox.opensandbox_runtime import _mount_spec_to_volume
    m = MountSpec(name="w", mount_path="/workspaces",
                  backend=HostMount(path="/host"))
    vol = _mount_spec_to_volume(m)
    assert vol == {
        "name": "w", "mountPath": "/workspaces",
        "host": {"path": "/host"},
    }


def test_opensandbox_translates_host_mount_read_only_and_subpath():
    from mini_cc.sandbox.opensandbox_runtime import _mount_spec_to_volume
    m = MountSpec(name="w", mount_path="/cfg",
                  backend=HostMount(path="/host/cfg"),
                  read_only=True, sub_path="subdir")
    vol = _mount_spec_to_volume(m)
    assert vol["readOnly"] is True
    assert vol["subPath"] == "subdir"


def test_opensandbox_translates_pvc_mount_defaults():
    """PVC with just claim_name → createIfNotExists=True default."""
    from mini_cc.sandbox.opensandbox_runtime import _mount_spec_to_volume
    m = MountSpec(name="w", mount_path="/data",
                  backend=PVCMount(claim_name="t1-pvc"))
    vol = _mount_spec_to_volume(m)
    assert vol["pvc"] == {
        "claimName": "t1-pvc",
        "createIfNotExists": True,
    }


def test_opensandbox_translates_pvc_mount_full():
    """PVC with all options → storageClass/storage only included when set."""
    from mini_cc.sandbox.opensandbox_runtime import _mount_spec_to_volume
    m = MountSpec(name="w", mount_path="/data",
                  backend=PVCMount(claim_name="t1-pvc",
                                   storage_class="fast-ssd",
                                   storage="5Gi",
                                   create_if_not_exists=False))
    vol = _mount_spec_to_volume(m)
    assert vol["pvc"] == {
        "claimName": "t1-pvc",
        "createIfNotExists": False,
        "storageClass": "fast-ssd",
        "storage": "5Gi",
    }


def test_opensandbox_translates_ossfs_mount():
    from mini_cc.sandbox.opensandbox_runtime import _mount_spec_to_volume
    m = MountSpec(name="oss", mount_path="/bucket",
                  backend=OSSFSMount(bucket="bkt", endpoint="oss-cn-hangzhou",
                                     access_key_id="AKID",
                                     access_key_secret="SECRET"))
    vol = _mount_spec_to_volume(m)
    assert vol["ossfs"] == {
        "bucket": "bkt", "endpoint": "oss-cn-hangzhou",
        "accessKeyId": "AKID", "accessKeySecret": "SECRET",
    }


def test_opensandbox_build_volumes_accepts_legacy_tuple():
    """_build_volumes handles mixed lists of MountSpec and legacy tuples
    (manager still passes tuples during the transition)."""
    from mini_cc.sandbox.opensandbox_runtime import _build_volumes
    vols = _build_volumes([
        MountSpec(name="w1", mount_path="/data",
                  backend=HostMount(path="/h1")),
        ("/h2", "/workspaces", ""),
    ])
    assert vols[0] == {"name": "w1", "mountPath": "/data",
                       "host": {"path": "/h1"}}
    assert vols[1]["mountPath"] == "/workspaces"
    assert vols[1]["host"] == {"path": "/h2"}
    assert vols[1]["name"].startswith("mnt")  # synthesized
