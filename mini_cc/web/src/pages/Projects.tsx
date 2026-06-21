import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import TopBar from "../components/TopBar";
import { ApiError, createProject, deleteProject, listProjects } from "../lib/api";
import { useAuth } from "../lib/store";
import type { ProjectOut } from "../lib/types";

export default function Projects() {
  const profile = useAuth((s) => s.current());
  const [items, setItems] = useState<ProjectOut[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [pid, setPid] = useState("");
  const [name, setName] = useState("");
  const [showCreate, setShowCreate] = useState(false);

  async function refresh() {
    if (!profile) return;
    setBusy(true);
    setError(null);
    try {
      const list = await listProjects(profile);
      setItems(list);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : (e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  useEffect(() => {
    refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [profile?.apiKey]);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!profile) return;
    try {
      await createProject(profile, {
        project_id: pid.trim() || undefined,
        display_name: name.trim() || undefined,
      });
      setPid("");
      setName("");
      setShowCreate(false);
      refresh();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : (e as Error).message);
    }
  }

  async function remove(p: ProjectOut) {
    if (!profile) return;
    if (!confirm(`delete project ${p.project_id}? this cannot be undone.`)) return;
    try {
      await deleteProject(profile, p.project_id);
      refresh();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : (e as Error).message);
    }
  }

  if (!profile) return null;

  return (
    <div className="min-h-screen">
      <TopBar title="projects" />
      <main className="max-w-5xl mx-auto px-6 py-8 space-y-6">
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-xl font-semibold">Projects</h1>
            <p className="text-sm text-ink-dim">
              tenant <span className="font-mono">{profile.tenantId}</span>
            </p>
          </div>
          <button
            onClick={() => setShowCreate((s) => !s)}
            className="px-4 py-2 bg-accent hover:bg-accent-hover text-white rounded font-medium"
          >
            new project
          </button>
        </div>

        {showCreate && (
          <form
            onSubmit={submit}
            className="bg-bg-card border border-border rounded-lg p-4 grid grid-cols-1 md:grid-cols-3 gap-3"
          >
            <input
              className="bg-bg rounded px-3 py-2 border border-border focus:border-accent outline-none font-mono"
              placeholder="project_id (optional)"
              value={pid}
              onChange={(e) => setPid(e.target.value)}
            />
            <input
              className="bg-bg rounded px-3 py-2 border border-border focus:border-accent outline-none md:col-span-2"
              placeholder="display name (optional)"
              value={name}
              onChange={(e) => setName(e.target.value)}
            />
            <div className="md:col-span-3 flex gap-2 justify-end">
              <button
                type="button"
                onClick={() => setShowCreate(false)}
                className="px-3 py-1.5 rounded border border-border"
              >
                cancel
              </button>
              <button type="submit" className="px-4 py-1.5 bg-accent text-white rounded">
                create
              </button>
            </div>
          </form>
        )}

        {error && (
          <div className="text-sm text-err bg-err/10 border border-err/40 rounded px-3 py-2">
            {error}
          </div>
        )}

        {busy ? (
          <div className="text-sm text-ink-dim">loading…</div>
        ) : items.length === 0 ? (
          <div className="text-sm text-ink-dim border border-dashed border-border rounded-lg p-12 text-center">
            no projects yet — click <span className="text-accent">new project</span> to create one
          </div>
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
            {items.map((p) => (
              <div
                key={p.project_id}
                className="group bg-bg-card border border-border rounded-lg p-4 hover:border-accent transition-colors flex flex-col gap-3"
              >
                <Link to={`/projects/${p.project_id}`} className="block space-y-1 flex-1">
                  <div className="font-medium">
                    {p.display_name || p.project_id}
                  </div>
                  <div className="text-xs text-ink-dim font-mono">{p.project_id}</div>
                  <div className="text-xs text-ink-faint">
                    created {new Date(p.created_at).toLocaleString()}
                  </div>
                </Link>
                <div className="flex justify-between items-center pt-2 border-t border-border">
                  <Link
                    to={`/projects/${p.project_id}`}
                    className="text-xs text-accent hover:underline"
                  >
                    open →
                  </Link>
                  <button
                    onClick={() => remove(p)}
                    className="text-xs text-err/80 hover:text-err"
                  >
                    delete
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}
      </main>
    </div>
  );
}
