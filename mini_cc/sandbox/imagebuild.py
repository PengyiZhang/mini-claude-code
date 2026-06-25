"""Render a Dockerfile from ContainerConfig and invoke ``docker build``.

Two paths:
- ``cfg.dockerfile_path`` set: use that file verbatim, ignore apt/pip/node.
  The operator owns the image entirely.
- Not set: take the package-shipped Dockerfile (mini_cc/sandbox/Dockerfile)
  and append ``RUN apt-get install ...``, ``RUN pip install ...``,
  ``RUN npm install -g ...`` layers for any declared packages.

Build context is always the package sandbox dir (so the base Dockerfile
is in context). With a custom Dockerfile, we still pass the package dir
as context to keep the implementation simple — operators needing a
custom context can build their image outside mini_cc and just set
``image_tag`` to the result.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from .config import ContainerConfig

_PKG_DIR = Path(__file__).resolve().parent
_DEFAULT_DOCKERFILE = _PKG_DIR / "Dockerfile"


class ImageBuildError(RuntimeError):
    pass


def _context_dir() -> Path:
    """Indirection for tests to swap in a temp dir."""
    return _PKG_DIR


def _default_dockerfile_text() -> str:
    return _DEFAULT_DOCKERFILE.read_text(encoding="utf-8")


def render_dockerfile(cfg: ContainerConfig) -> str:
    """Return the Dockerfile text for this config.

    Uses ``cfg.dockerfile_path`` verbatim if set; otherwise layers
    declarative packages on top of the package default Dockerfile.
    """
    if cfg.dockerfile_path:
        return Path(cfg.dockerfile_path).read_text(encoding="utf-8")

    base = _default_dockerfile_text()
    lines = base.splitlines()
    cmd_idx = next(
        (i for i, ln in enumerate(lines) if ln.strip().startswith("CMD")),
        len(lines))
    head = lines[:cmd_idx]
    tail = lines[cmd_idx:]

    layers: list[str] = []
    if cfg.apt_packages:
        pkgs = " ".join(cfg.apt_packages)
        layers.append(
            f"RUN apt-get update && apt-get install -y --no-install-recommends "
            f"{pkgs} && rm -rf /var/lib/apt/lists/*")
    if cfg.pip_packages:
        pkgs = " ".join(cfg.pip_packages)
        layers.append(f"RUN pip install --no-cache-dir {pkgs}")
    if cfg.node_packages:
        pkgs = " ".join(cfg.node_packages)
        layers.append(f"RUN npm install -g {pkgs} && npm cache clean --force")

    return "\n".join(head + layers + tail) + "\n"


def build_image(tag: str, cfg: ContainerConfig, *,
                prefix: tuple[str, ...] | list[str] = ()) -> None:
    """Write the rendered Dockerfile to the build context and invoke docker.

    When ``cfg.dockerfile_path`` is set, that file is used verbatim as
    the ``-f`` argument (no copy). Otherwise the rendered Dockerfile is
    written to ``Dockerfile.rendered`` in the context dir.

    ``prefix`` is prepended to the ``docker build`` call (e.g.
    ``("wsl",)`` when docker lives inside a WSL2 distro). The build
    context + dockerfile paths are translated to their WSL ``/mnt/``
    view in that case so the in-WSL daemon can read them.

    Raises ImageBuildError on non-zero exit. Caller decides whether to
    fail the request or degrade.
    """
    from .osdetect import docker_argv, to_wsl_path
    ctx = _context_dir()
    if cfg.dockerfile_path:
        # User owns the image; point -f straight at their file.
        dockerfile_arg = cfg.dockerfile_path
    else:
        dockerfile_text = render_dockerfile(cfg)
        out_path = ctx / "Dockerfile.rendered"
        out_path.write_text(dockerfile_text, encoding="utf-8")
        dockerfile_arg = str(out_path)
    # When docker runs inside WSL it can only see the Windows filesystem
    # through /mnt/<drive>/... — translate both the context and the
    # Dockerfile path so the build actually finds them.
    if prefix:
        dockerfile_arg = to_wsl_path(dockerfile_arg)
        ctx_arg = to_wsl_path(str(ctx))
    else:
        ctx_arg = str(ctx)
    args = docker_argv(prefix, "build", "-t", tag, "-f", dockerfile_arg, ctx_arg)
    cp = subprocess.run(args, capture_output=True, text=True, timeout=600)
    if cp.returncode != 0:
        raise ImageBuildError(
            f"docker build failed (exit {cp.returncode}): "
            f"{(cp.stderr or cp.stdout).strip()[:500]}")
