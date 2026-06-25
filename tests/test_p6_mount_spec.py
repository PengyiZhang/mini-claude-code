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
