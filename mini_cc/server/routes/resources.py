"""Resource routes: file tree browse, upload (file/folder), download zip.

All paths are resolved relative to the project workspace. Escapes (``..``,
absolute paths, symlinks pointing outside) raise 400. Cross-tenant access
returns 404 — same model as the other routes.
"""
from __future__ import annotations

import io
import os
import zipfile
from pathlib import Path, PurePosixPath
from typing import List

from fastapi import (APIRouter, Depends, File, Form, Path as FPath, Query,
                     Request, UploadFile)
from fastapi.responses import StreamingResponse

from ..deps import get_pm, require_scope, validate_id
from ..errors import BadRequest, NotFound

router = APIRouter(
    prefix="/tenants/{tid}/projects/{pid}/files",
    tags=["resources"],
)

_TEXT_EXTS = {
    ".txt", ".md", ".markdown", ".py", ".js", ".ts", ".tsx", ".jsx",
    ".json", ".jsonl", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".env",
    ".sh", ".bash", ".zsh", ".ps1", ".bat", ".cmd",
    ".html", ".css", ".scss", ".less",
    ".java", ".c", ".cc", ".cpp", ".h", ".hpp", ".go", ".rs",
    ".rb", ".php", ".swift", ".kt", ".scala",
    ".sql", ".xml", ".csv", ".log", ".conf",
}
MAX_PREVIEW_BYTES = 256 * 1024


def _resolve_ws(project) -> Path:
    return Path(project.workspace).resolve()


def _safe_rel(project, rel: str | None) -> PurePosixPath:
    """Resolve a user-supplied relative path inside the workspace,
    rejecting escapes. Returns a PurePosixPath relative to workspace."""
    rel = (rel or "").strip().lstrip("/")
    if not rel:
        return PurePosixPath(".")
    p = PurePosixPath(rel)
    parts = []
    for part in p.parts:
        if part in ("", "."):
            continue
        if part == "..":
            raise BadRequest(f"path escape detected: {rel!r}")
        if "\\" in part or "\x00" in part:
            raise BadRequest(f"invalid characters in path: {rel!r}")
        parts.append(part)
    return PurePosixPath(*parts) if parts else PurePosixPath(".")


def _full_path(project, rel: PurePosixPath) -> Path:
    ws = _resolve_ws(project)
    full = (ws / rel).resolve()
    try:
        full.relative_to(ws)
    except ValueError:
        raise BadRequest("path escapes workspace")
    return full


def _check(project, pid: str, tid: str):
    if project.meta.tenant_id != tid:
        raise NotFound(f"project {pid} not found")


@router.get("/tree")
def list_tree(pid: str = FPath(...),
              tid: str = Depends(require_scope("files:read")),
              pm=Depends(get_pm),
              path: str = Query(default=""),
              include_hidden: bool | None = Query(default=None)) -> List[dict]:
    """List immediate children of ``path`` (relative to workspace root).

    Returns a list of ``{name, path, is_dir, size, modified}`` dicts.
    Hidden entries (starting with ``.``) are hidden by default to keep
    the UI clean. Operators can flip the default server-wide via the
    ``MINI_CC_TREE_SHOW_HIDDEN`` env var (``"1"``/``"true"`` to show),
    and clients can override per-request with ``?include_hidden=true``.
    """
    validate_id(pid)
    try:
        project = pm.get(pid)
    except KeyError:
        raise NotFound(f"project {pid} not found")
    _check(project, pid, tid)

    # Per-request override wins; otherwise fall back to the operator's
    # default; otherwise hide. Truthy strings: "1", "true", "yes" (case
    # insensitive). Empty / anything else = hide.
    if include_hidden is None:
        raw = os.environ.get("MINI_CC_TREE_SHOW_HIDDEN", "").strip().lower()
        include_hidden = raw in ("1", "true", "yes", "on")

    rel = _safe_rel(project, path)
    full = _full_path(project, rel)
    if not full.exists():
        raise NotFound(f"path not found: {path!r}")
    if not full.is_dir():
        raise BadRequest(f"not a directory: {path!r}")

    out = []
    for child in sorted(full.iterdir(), key=lambda c: (not c.is_dir(), c.name.lower())):
        if child.name.startswith(".") and not include_hidden:
            continue
        try:
            st = child.stat()
        except OSError:
            continue
        out.append({
            "name": child.name,
            "path": str((rel / child.name) if str(rel) != "." else PurePosixPath(child.name)),
            "is_dir": child.is_dir(),
            "size": st.st_size if child.is_file() else 0,
            "modified": int(st.st_mtime),
        })
    return out


@router.get("/content")
def read_content(pid: str = FPath(...),
                 tid: str = Depends(require_scope("files:read")),
                 pm=Depends(get_pm),
                 path: str = Query(...)) -> dict:
    """Read up to 256 KB of a file's content. Binary or oversized files
    return metadata only (no ``content``)."""
    validate_id(pid)
    try:
        project = pm.get(pid)
    except KeyError:
        raise NotFound(f"project {pid} not found")
    _check(project, pid, tid)

    rel = _safe_rel(project, path)
    full = _full_path(project, rel)
    if not full.exists() or not full.is_file():
        raise NotFound(f"file not found: {path!r}")

    st = full.stat()
    is_text = full.suffix.lower() in _TEXT_EXTS
    body: str | None = None
    truncated = False
    if is_text and st.st_size <= MAX_PREVIEW_BYTES:
        try:
            body = full.read_text(encoding="utf-8", errors="replace")
        except OSError:
            body = None
    elif is_text:
        truncated = True
        try:
            with full.open("rb") as fp:
                body = fp.read(MAX_PREVIEW_BYTES).decode("utf-8", "replace")
        except OSError:
            body = None
    return {
        "path": str(rel),
        "name": full.name,
        "size": st.st_size,
        "modified": int(st.st_mtime),
        "is_text": is_text,
        "truncated": truncated,
        "content": body,
    }


@router.post("/mkdir")
def make_dir(pid: str = FPath(...),
             tid: str = Depends(require_scope("files:write")),
             pm=Depends(get_pm),
             path: str = Query(...)) -> dict:
    validate_id(pid)
    try:
        project = pm.get(pid)
    except KeyError:
        raise NotFound(f"project {pid} not found")
    _check(project, pid, tid)
    rel = _safe_rel(project, path)
    full = _full_path(project, rel)
    full.mkdir(parents=True, exist_ok=True)
    return {"path": str(rel), "created": True}


@router.post("/upload")
async def upload_files(pid: str = FPath(...),
                       tid: str = Depends(require_scope("files:write")),
                       pm=Depends(get_pm),
                       path: str = Query(default=""),
                       files: List[UploadFile] = File(...),
                       rel_paths: List[str] = Form(default=[])) -> dict:
    """Upload one or more files, optionally preserving folder structure.

    For folder uploads, the browser sends each file's relative path
    (via ``webkitdirectory``) in the ``rel_paths`` form field, parallel
    to ``files``. Missing rel_paths fall back to the bare filename.
    """
    validate_id(pid)
    try:
        project = pm.get(pid)
    except KeyError:
        raise NotFound(f"project {pid} not found")
    _check(project, pid, tid)

    base_rel = _safe_rel(project, path)
    base_full = _full_path(project, base_rel)
    base_full.mkdir(parents=True, exist_ok=True)

    saved = []
    for i, upload in enumerate(files):
        rel = rel_paths[i] if i < len(rel_paths) and rel_paths[i] else upload.filename
        target_rel = _safe_rel(project, str(base_rel / rel) if str(base_rel) != "." else rel)
        target_full = _full_path(project, target_rel)
        target_full.parent.mkdir(parents=True, exist_ok=True)
        with target_full.open("wb") as out:
            while True:
                chunk = await upload.read(64 * 1024)
                if not chunk:
                    break
                out.write(chunk)
        saved.append({"path": str(target_rel), "size": target_full.stat().st_size})
    return {"uploaded": len(saved), "files": saved}


@router.delete("")
def delete_path(pid: str = FPath(...),
                tid: str = Depends(require_scope("files:write")),
                pm=Depends(get_pm),
                path: str = Query(...)) -> dict:
    validate_id(pid)
    try:
        project = pm.get(pid)
    except KeyError:
        raise NotFound(f"project {pid} not found")
    _check(project, pid, tid)
    rel = _safe_rel(project, path)
    full = _full_path(project, rel)
    if not full.exists():
        raise NotFound(f"path not found: {path!r}")
    import shutil
    if full.is_dir():
        shutil.rmtree(full)
    else:
        full.unlink()
    return {"deleted": str(rel)}


# Download project as ZIP — mounted at /tenants/{tid}/projects/{pid}/download
download_router = APIRouter(
    prefix="/tenants/{tid}/projects",
    tags=["resources"],
)


@download_router.get("/{pid}/download")
def download_zip(pid: str = FPath(...),
                 tid: str = Depends(require_scope("files:read")),
                 pm=Depends(get_pm)) -> StreamingResponse:
    validate_id(pid)
    try:
        project = pm.get(pid)
    except KeyError:
        raise NotFound(f"project {pid} not found")
    _check(project, pid, tid)
    ws = _resolve_ws(project)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        if ws.exists():
            for p in ws.rglob("*"):
                if any(part.startswith(".") for part in p.relative_to(ws).parts[:-1]):
                    continue
                if p.is_file():
                    zf.write(p, p.relative_to(project.root))
    buf.seek(0)

    safe_name = pid.replace("/", "_")

    def iterate():
        while True:
            chunk = buf.read(64 * 1024)
            if not chunk:
                break
            yield chunk

    return StreamingResponse(
        iterate(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{safe_name}.zip"'},
    )
