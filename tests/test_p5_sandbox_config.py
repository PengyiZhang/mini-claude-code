"""ContainerConfig + sandbox.toml loader. Uses tmp_path for fixture files."""
from __future__ import annotations

import pytest

from mini_cc.sandbox.config import (
    ContainerConfig, load_tenant_config, resolve_kind,
    DEFAULT_IMAGE_TAG)


def test_defaults():
    cfg = ContainerConfig()
    assert cfg.enabled is False
    assert cfg.image_tag == DEFAULT_IMAGE_TAG
    assert cfg.network == "none"
    assert cfg.dockerfile_path is None
    assert cfg.apt_packages == []
    assert cfg.pip_packages == []
    assert cfg.node_packages == []
    assert cfg.extra_mounts == []


def test_load_tenant_config_missing_file(tmp_path):
    tenants_dir = tmp_path / "tenants"
    tenants_dir.mkdir()
    cfg = load_tenant_config("t1", tenants_dir)
    assert cfg is None


def test_load_tenant_config_minimal(tmp_path):
    tenants_dir = tmp_path / "tenants" / "t1"
    tenants_dir.mkdir(parents=True)
    (tenants_dir / "sandbox.toml").write_text('enabled = true\n')
    cfg = load_tenant_config("t1", tmp_path / "tenants")
    assert cfg is not None
    assert cfg.enabled is True
    assert cfg.image_tag == DEFAULT_IMAGE_TAG


def test_load_tenant_config_full(tmp_path):
    tenants_dir = tmp_path / "tenants" / "t2"
    tenants_dir.mkdir(parents=True)
    (tenants_dir / "sandbox.toml").write_text(
        'enabled = true\n'
        'image_tag = "my-registry/sandbox:v3"\n'
        'network = "bridge"\n'
        'dockerfile_path = "./sandbox/Dockerfile.custom"\n'
        'cpu_quota = "1.5"\n'
        'memory_limit = "1g"\n'
        '\n'
        'apt_packages = ["ffmpeg", "imagemagick"]\n'
        'pip_packages = ["numpy", "pandas"]\n'
        'node_packages = ["typescript"]\n'
        '\n'
        '[[extra_mounts]]\n'
        'host = "/host/cache"\n'
        'container = "/cache"\n'
        'options = "ro"\n'
        '\n'
        '[[extra_mounts]]\n'
        'host = "/host/pip-cache"\n'
        'container = "/root/.cache/pip"\n'
    )
    cfg = load_tenant_config("t2", tmp_path / "tenants")
    assert cfg is not None
    assert cfg.image_tag == "my-registry/sandbox:v3"
    assert cfg.network == "bridge"
    assert cfg.dockerfile_path == "./sandbox/Dockerfile.custom"
    assert cfg.cpu_quota == "1.5"
    assert cfg.memory_limit == "1g"
    assert cfg.apt_packages == ["ffmpeg", "imagemagick"]
    assert cfg.pip_packages == ["numpy", "pandas"]
    assert cfg.node_packages == ["typescript"]
    assert len(cfg.extra_mounts) == 2
    assert cfg.extra_mounts[0].host == "/host/cache"
    assert cfg.extra_mounts[0].container == "/cache"
    assert cfg.extra_mounts[0].options == "ro"
    assert cfg.extra_mounts[1].options == ""


def test_load_tenant_config_invalid_network(tmp_path):
    tenants_dir = tmp_path / "tenants" / "t3"
    tenants_dir.mkdir(parents=True)
    (tenants_dir / "sandbox.toml").write_text(
        'enabled = true\nnetwork = "host"\n')
    with pytest.raises(ValueError, match="network"):
        load_tenant_config("t3", tmp_path / "tenants")


def test_load_tenant_config_tenant_id_traversal_rejected(tmp_path):
    tenants_dir = tmp_path / "tenants"
    tenants_dir.mkdir()
    with pytest.raises(ValueError):
        load_tenant_config("../escape", tenants_dir)


def test_resolve_kind_disabled_by_default(monkeypatch, tmp_path):
    monkeypatch.delenv("MINI_CC_SANDBOX_DEFAULT", raising=False)
    kind, cfg = resolve_kind("t1", tmp_path / "tenants")
    assert kind == "subprocess"
    assert cfg is None


def test_resolve_kind_env_container_overrides_absent_tenant_file(monkeypatch, tmp_path):
    monkeypatch.setenv("MINI_CC_SANDBOX_DEFAULT", "container")
    kind, cfg = resolve_kind("t1", tmp_path / "tenants")
    assert kind == "container"
    assert cfg is not None
    assert cfg.enabled is True
    assert cfg.image_tag == DEFAULT_IMAGE_TAG


def test_resolve_kind_tenant_file_overrides_env(monkeypatch, tmp_path):
    monkeypatch.setenv("MINI_CC_SANDBOX_DEFAULT", "container")
    tenants_dir = tmp_path / "tenants" / "t1"
    tenants_dir.mkdir(parents=True)
    (tenants_dir / "sandbox.toml").write_text('enabled = false\n')
    kind, cfg = resolve_kind("t1", tmp_path / "tenants")
    assert kind == "subprocess"
    assert cfg is None


def test_resolve_kind_invalid_env_value(monkeypatch, tmp_path):
    monkeypatch.setenv("MINI_CC_SANDBOX_DEFAULT", "kubernetes")
    with pytest.raises(ValueError):
        resolve_kind("t1", tmp_path / "tenants")
