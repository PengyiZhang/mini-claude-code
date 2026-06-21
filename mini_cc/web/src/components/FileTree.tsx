import { useEffect, useState } from "react";
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
}: {
  pid: string;
  onPickFile: (p: string) => void;
  reloadKey: number;
}) {
  return (
    <div className="font-mono text-sm">
      <DirChildren
        pid={pid}
        path=""
        depth={0}
        onPickFile={onPickFile}
        onChildChanged={() => {
          /* root re-mount via key */
        }}
        onRefreshKey={reloadKey}
      />
    </div>
  );
}
