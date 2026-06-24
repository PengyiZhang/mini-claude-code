"""Dockerfile rendering + build orchestrator. The actual docker build
call is patched out in every test."""
from __future__ import annotations

import subprocess

import pytest

from mini_cc.sandbox.config import ContainerConfig, Mount
from mini_cc.sandbox.imagebuild import (
    render_dockerfile, build_image, ImageBuildError)


def test_render_default_no_packages():
    df = render_dockerfile(ContainerConfig())
    assert "FROM python:3.10-slim" in df
    assert "CMD" in df and "sleep" in df and "infinity" in df
    # No *extra* install layers appended (base Dockerfile still has its own
    # apt-get for git/ripgrep and one for nodejs — both stay).
    assert "pip install" not in df
    assert "npm install -g" not in df


def test_render_with_apt_packages():
    cfg = ContainerConfig(apt_packages=["ffmpeg", "imagemagick"])
    df = render_dockerfile(cfg)
    assert "ffmpeg imagemagick" in df
    assert "apt-get install" in df


def test_render_with_pip_packages():
    cfg = ContainerConfig(pip_packages=["numpy", "pandas"])
    df = render_dockerfile(cfg)
    assert "pip install" in df
    assert "numpy pandas" in df


def test_render_with_node_packages():
    cfg = ContainerConfig(node_packages=["typescript", "tsx"])
    df = render_dockerfile(cfg)
    assert "npm install -g" in df
    assert "typescript tsx" in df


def test_render_all_dimensions_combined():
    cfg = ContainerConfig(
        apt_packages=["ffmpeg"],
        pip_packages=["numpy"],
        node_packages=["typescript"])
    df = render_dockerfile(cfg)
    apt_pos = df.index("apt-get install")
    pip_pos = df.index("pip install")
    node_pos = df.index("npm install -g")
    cmd_pos = df.index("CMD")
    assert apt_pos < pip_pos < node_pos < cmd_pos


def test_build_image_success(monkeypatch, tmp_path):
    captured = []
    def fake_run(args, **kw):
        captured.append(args)
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("mini_cc.sandbox.imagebuild._context_dir", lambda: tmp_path)

    build_image("my-tag", ContainerConfig())

    assert captured[0][:3] == ["docker", "build", "-t"]
    assert captured[0][3] == "my-tag"
    assert captured[0][-1] == str(tmp_path)


def test_build_image_failure_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(subprocess, "run",
        lambda args, **kw: subprocess.CompletedProcess(
            args=args, returncode=1, stdout="", stderr="apt-get failed"))
    monkeypatch.setattr("mini_cc.sandbox.imagebuild._context_dir", lambda: tmp_path)
    with pytest.raises(ImageBuildError, match="apt-get failed"):
        build_image("bad-tag", ContainerConfig())


def test_build_image_with_custom_dockerfile(monkeypatch, tmp_path):
    custom = tmp_path / "Dockerfile.custom"
    custom.write_text("FROM alpine\nRUN echo hi\n")
    captured = []
    monkeypatch.setattr(subprocess, "run",
        lambda args, **kw: captured.append(args) or
            subprocess.CompletedProcess(args=args, returncode=0))
    monkeypatch.setattr("mini_cc.sandbox.imagebuild._context_dir", lambda: tmp_path)

    build_image("custom-tag", ContainerConfig(
        dockerfile_path=str(custom),
        apt_packages=["should-be-ignored"]))

    assert "-f" in captured[0]
    f_idx = captured[0].index("-f")
    assert captured[0][f_idx + 1] == str(custom)


def test_render_uses_base_repo_dockerfile_when_no_path():
    df = render_dockerfile(ContainerConfig())
    assert "ripgrep" in df
    assert "nodesource" in df or "nodejs" in df
