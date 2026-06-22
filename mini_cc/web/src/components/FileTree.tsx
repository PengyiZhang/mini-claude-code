import { useEffect, useRef, useState } from "react";
import { ApiError, deletePath, listTree, mkdir, uploadFiles } from "../lib/api";
import { useAuth } from "../lib/store";
import type { TreeNode } from "../lib/types";

interface NodeProps {
  pid: string;
  path: string;
  depth: number;
  onPickFile: (path: string) => void;
  onRefreshKey: number;
  onChildChanged: () => void;
}

function DirChildren({ pid, path, onPickFile, onChildChanged, depth, onRefreshKey }: NodeProps) {
  const profile = useAuth((s) => s.current())!;
  const [items, setItems] = useState<TreeNode[]>([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);

  async function load() {
    setLoading(true);
    setErr(null);
    try {
      const list = await listTree(profile, pid, path);
      setItems(list);
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : (e as Error).message);
    } finally {
      setLoading(false);
    }
  }

  async function reload() {
    // Reload without unmounting children (setLoading(true) would early-return
    // and lose local TreeRow state like expanded folders).
    setErr(null);
    try {
      const list = await listTree(profile, pid, path);
      setItems(list);
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : (e as Error).message);
    }
  }

  // Watch onRefreshKey (sourced from the parent's reloadKey) so the tree
  // re-fetches when an external action (sidebar upload, mkdir via API,
  // etc.) bumps it. Previously this component declared its own local
  // reloadKey state that nothing ever changed, so the prop was silently
  // ignored — root-level uploads didn't appear without a manual refresh.
  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [onRefreshKey]);

  function childChanged() {
    reload();
    onChildChanged();
  }

  if (loading) return <div className="text-xs text-ink-faint pl-3">loading…</div>;
  if (err) return <div className="text-xs text-err pl-3">{err}</div>;
  if (items.length === 0) return <div className="text-xs text-ink-faint pl-3 italic">empty</div>;

  return (
    <div>
      {items.map((it) => (
        <TreeRow
          key={it.path}
          pid={pid}
          node={it}
          depth={depth}
          onPickFile={onPickFile}
          onChildChanged={childChanged}
        />
      ))}
    </div>
  );
}

function TreeRow({
  pid,
  node,
  depth,
  onPickFile,
  onChildChanged,
}: {
  pid: string;
  node: TreeNode;
  depth: number;
  onPickFile: (p: string) => void;
  onChildChanged: () => void;
}) {
  const profile = useAuth((s) => s.current())!;
  const [expanded, setExpanded] = useState(false);
  const [menu, setMenu] = useState<{ x: number; y: number } | null>(null);
  const [uploading, setUploading] = useState(false);
  const [uploadErr, setUploadErr] = useState<string | null>(null);
  const [creatingDir, setCreatingDir] = useState(false);
  const [newDirName, setNewDirName] = useState("");
  const [deleting, setDeleting] = useState(false);

  useEffect(() => {
    if (!menu) return;
    const close = () => setMenu(null);
    window.addEventListener("click", close);
    window.addEventListener("contextmenu", close);
    return () => {
      window.removeEventListener("click", close);
      window.removeEventListener("contextmenu", close);
    };
  }, [menu]);

  function onCtx(e: React.MouseEvent) {
    e.preventDefault();
    e.stopPropagation();
    setMenu({ x: e.clientX, y: e.clientY });
  }

  function triggerUpload(kind: "file" | "folder") {
    const input = document.createElement("input");
    input.type = "file";
    input.multiple = true;
    input.style.display = "none";
    if (kind === "folder") {
      (input as HTMLInputElement & { webkitdirectory: boolean }).webkitdirectory = true;
    }
    input.onchange = async () => {
      if (!input.files || input.files.length === 0) {
        input.remove();
        return;
      }
      setUploading(true);
      setUploadErr(null);
      const files: File[] = [];
      const rels: string[] = [];
      for (const f of Array.from(input.files)) {
        files.push(f);
        const rel: string =
          (f as File & { webkitRelativePath?: string }).webkitRelativePath || f.name;
        // Strip the top-level folder name when uploading a folder (so we
        // preserve its contents but don't create a redundant parent).
        const cleaned = kind === "folder" ? rel.split("/").slice(1).join("/") || rel : rel;
        rels.push(cleaned);
      }
      try {
        await uploadFiles(profile, pid, node.path, files, rels);
        onChildChanged();
        if (node.is_dir) setExpanded(true);
      } catch (e) {
        setUploadErr(e instanceof ApiError ? e.message : (e as Error).message);
      } finally {
        setUploading(false);
        input.remove();
      }
    };
    document.body.appendChild(input);
    input.click();
  }

  async function confirmMkdir() {
    if (!newDirName.trim()) {
      setCreatingDir(false);
      return;
    }
    try {
      const childPath = node.is_dir ? `${node.path}/${newDirName.trim()}` : newDirName.trim();
      await mkdir(profile, pid, childPath);
      setNewDirName("");
      setCreatingDir(false);
      setExpanded(true);
      onChildChanged();
    } catch (e) {
      setUploadErr(e instanceof ApiError ? e.message : (e as Error).message);
    }
  }

  async function doDelete() {
    if (!confirm(`delete ${node.path}?`)) return;
    setDeleting(true);
    try {
      await deletePath(profile, pid, node.path);
      onChildChanged();
    } catch (e) {
      setUploadErr(e instanceof ApiError ? e.message : (e as Error).message);
    } finally {
      setDeleting(false);
    }
  }

  const pad = { paddingLeft: `${depth * 12 + 12}px` };

  return (
    <div>
      <div
        className="flex items-center gap-2 py-1 pr-2 text-sm cursor-pointer hover:bg-bg-hover rounded"
        style={pad}
        onClick={() => {
          if (node.is_dir) setExpanded((e) => !e);
          else onPickFile(node.path);
        }}
        onContextMenu={onCtx}
      >
        {node.is_dir ? (
          <span className="text-ink-dim text-xs w-3">{expanded ? "▼" : "▶"}</span>
        ) : (
          <span className="w-3" />
        )}
        <span className={node.is_dir ? "text-accent" : "text-ink"}>{node.is_dir ? "📁" : "📄"}</span>
        <span className="truncate flex-1">{node.name}</span>
        {!node.is_dir && (
          <span className="text-xs text-ink-faint">{formatSize(node.size)}</span>
        )}
        {uploading && <span className="text-xs text-accent animate-pulse">uploading…</span>}
        {deleting && <span className="text-xs text-err animate-pulse">deleting…</span>}
      </div>

      {creatingDir && (
        <div className="flex items-center gap-2 py-1" style={{ paddingLeft: `${depth * 12 + 36}px` }}>
          <input
            autoFocus
            value={newDirName}
            onChange={(e) => setNewDirName(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") confirmMkdir();
              if (e.key === "Escape") setCreatingDir(false);
            }}
            placeholder="new-folder"
            className="text-sm bg-bg border border-accent rounded px-2 py-0.5 outline-none"
          />
        </div>
      )}

      {uploadErr && (
        <div className="text-xs text-err pl-4">{uploadErr}</div>
      )}

      {expanded && node.is_dir && (
        <DirChildren
          pid={pid}
          path={node.path}
          depth={depth + 1}
          onPickFile={onPickFile}
          onChildChanged={onChildChanged}
          onRefreshKey={0}
        />
      )}

      {menu && (
        <div
          className="fixed z-50 bg-bg-card border border-border shadow-xl rounded-md text-sm min-w-[180px] py-1"
          style={{ left: menu.x, top: menu.y }}
          onClick={(e) => e.stopPropagation()}
        >
          {node.is_dir && (
            <>
              <MenuItem
                onClick={() => {
                  setMenu(null);
                  triggerUpload("file");
                }}
              >
                📄 Upload files…
              </MenuItem>
              <MenuItem
                onClick={() => {
                  setMenu(null);
                  triggerUpload("folder");
                }}
              >
                📁 Upload folder…
              </MenuItem>
              <MenuItem
                onClick={() => {
                  setMenu(null);
                  setCreatingDir(true);
                }}
              >
                ＋ New folder…
              </MenuItem>
              <div className="h-px bg-border my-1" />
            </>
          )}
          {!node.is_dir && (
            <MenuItem
              onClick={() => {
                setMenu(null);
                onPickFile(node.path);
              }}
            >
              👁 Preview
            </MenuItem>
          )}
          <MenuItem
            onClick={() => {
              setMenu(null);
              doDelete();
            }}
            danger
          >
            🗑 Delete
          </MenuItem>
        </div>
      )}
    </div>
  );
}

function MenuItem({
  children,
  onClick,
  danger,
}: {
  children: React.ReactNode;
  onClick: () => void;
  danger?: boolean;
}) {
  return (
    <div
      onClick={onClick}
      className={`px-3 py-1.5 cursor-pointer hover:bg-bg-hover ${
        danger ? "text-err" : "text-ink"
      }`}
    >
      {children}
    </div>
  );
}

function formatSize(n: number) {
  if (n < 1024) return `${n}B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)}K`;
  return `${(n / 1024 / 1024).toFixed(1)}M`;
}

export default function FileTree({
  pid,
  onPickFile,
  reloadKey,
  onDownloadZip,
}: {
  pid: string;
  onPickFile: (p: string) => void;
  reloadKey: number;
  onDownloadZip: () => void;
}) {
  const profile = useAuth((s) => s.current())!;
  const [menuOpen, setMenuOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [creatingRootDir, setCreatingRootDir] = useState(false);
  const [newDirName, setNewDirName] = useState("");
  const menuRef = useRef<HTMLDivElement | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const folderInputRef = useRef<HTMLInputElement | null>(null);

  // Close the "+" dropdown on outside click. Without this the menu hangs
  // around after picking a file, hiding the tree behind it.
  useEffect(() => {
    if (!menuOpen) return;
    const onDoc = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) {
        setMenuOpen(false);
      }
    };
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, [menuOpen]);

  // Hidden file inputs — one for files, one for folders (webkitdirectory
  // can't be toggled on a single input without a remount, so we keep two).
  // The onChange handler is set each time we open the picker so we can
  // distinguish which one fired without React state races.

  async function doUpload(kind: "file" | "folder", fileList: FileList | null) {
    if (!fileList || fileList.length === 0) return;
    setBusy(true);
    setErr(null);
    try {
      const files: File[] = [];
      const rels: string[] = [];
      for (const f of Array.from(fileList)) {
        files.push(f);
        const rel: string =
          (f as File & { webkitRelativePath?: string }).webkitRelativePath || f.name;
        // Strip the top-level folder name for folder uploads (matches
        // TreeRow behavior) so we get the contents, not a redundant parent.
        const cleaned = kind === "folder" ? rel.split("/").slice(1).join("/") || rel : rel;
        rels.push(cleaned);
      }
      await uploadFiles(profile, pid, "", files, rels);
      // Bump reloadKey via onPickFile no-op hack? No — refresh by reloading
      // the DirChildren which watches reloadKey. The parent owns reloadKey.
      // We trigger it by calling a hidden handler.
      bumpRootReload();
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : (e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  // Trigger reload by bumping a local state that we pass down as
  // onRefreshKey. The parent's reloadKey still wins, but for in-tree
  // actions the parent doesn't know we changed anything, so we keep a
  // local counter that we can bump independently.
  const [localReload, setLocalReload] = useState(0);
  function bumpRootReload() {
    setLocalReload((n) => n + 1);
  }

  async function confirmRootMkdir() {
    if (!newDirName.trim()) {
      setCreatingRootDir(false);
      return;
    }
    setBusy(true);
    setErr(null);
    try {
      await mkdir(profile, pid, newDirName.trim());
      setNewDirName("");
      setCreatingRootDir(false);
      bumpRootReload();
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : (e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  // Combined refresh key: parent (external uploads) + local (in-tree actions).
  const refreshKey = reloadKey + localReload;

  return (
    <div className="font-mono text-sm">
      {/* Header bar: workspace title + actions. Replaces the three ugly
          sidebar buttons and gives the tree a proper top-level affordance. */}
      <div className="flex items-center justify-between mb-2 px-1">
        <span className="text-xs text-ink-dim uppercase tracking-wide">workspace</span>
        <div className="flex items-center gap-1 relative">
          <button
            onClick={() => setMenuOpen((v) => !v)}
            disabled={busy}
            title="add to workspace"
            className="text-sm px-2 py-0.5 rounded border border-border hover:border-accent hover:text-accent disabled:opacity-50"
          >
            {busy ? "…" : "＋"}
          </button>
          <button
            onClick={onDownloadZip}
            disabled={busy}
            title="download workspace as ZIP"
            className="text-sm px-2 py-0.5 rounded border border-border hover:border-accent hover:text-accent disabled:opacity-50"
          >
            ⬇
          </button>
          {menuOpen && (
            <div
              ref={menuRef}
              className="absolute right-0 top-7 z-50 bg-bg-card border border-border shadow-xl rounded-md text-sm min-w-[180px] py-1"
            >
              <div
                onClick={() => {
                  setMenuOpen(false);
                  fileInputRef.current?.click();
                }}
                className="px-3 py-1.5 cursor-pointer hover:bg-bg-hover text-ink"
              >
                📄 Upload files…
              </div>
              <div
                onClick={() => {
                  setMenuOpen(false);
                  folderInputRef.current?.click();
                }}
                className="px-3 py-1.5 cursor-pointer hover:bg-bg-hover text-ink"
              >
                📁 Upload folder…
              </div>
              <div
                onClick={() => {
                  setMenuOpen(false);
                  setCreatingRootDir(true);
                }}
                className="px-3 py-1.5 cursor-pointer hover:bg-bg-hover text-ink"
              >
                ＋ New folder…
              </div>
            </div>
          )}
        </div>
      </div>

      {creatingRootDir && (
        <div className="flex items-center gap-2 py-1 mb-2" style={{ paddingLeft: "12px" }}>
          <input
            autoFocus
            value={newDirName}
            onChange={(e) => setNewDirName(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") confirmRootMkdir();
              if (e.key === "Escape") setCreatingRootDir(false);
            }}
            placeholder="new-folder"
            className="text-sm bg-bg border border-accent rounded px-2 py-0.5 outline-none"
          />
        </div>
      )}

      {err && <div className="text-xs text-err pl-3 mb-2">{err}</div>}

      {/* Hidden inputs for the + menu. Kept outside the menu so the menu
          can close before the picker opens (otherwise the picker would
          close the menu on Firefox). */}
      <input
        ref={fileInputRef}
        type="file"
        multiple
        className="hidden"
        onChange={(e) => {
          void doUpload("file", e.target.files);
          e.target.value = "";
        }}
      />
      <input
        ref={(el) => {
          folderInputRef.current = el;
          if (el) {
            // webkitdirectory isn't in React's TS types; assign via DOM.
            (el as HTMLInputElement & { webkitdirectory: boolean }).webkitdirectory = true;
          }
        }}
        type="file"
        multiple
        className="hidden"
        onChange={(e) => {
          void doUpload("folder", e.target.files);
          e.target.value = "";
        }}
      />

      <DirChildren
        pid={pid}
        path=""
        depth={0}
        onPickFile={onPickFile}
        onChildChanged={bumpRootReload}
        onRefreshKey={refreshKey}
      />
    </div>
  );
}
