import { useCallback, useEffect, useState } from "react";
import type { TenantProfile } from "../lib/types";
import {
  getRunTableWorkflows,
  getRunTableBackground,
  type WorkflowsOut,
  type BackgroundTaskOut,
} from "../lib/api";
import { runCommandOnce } from "../lib/commands";

/**
 * Sidebar "Run Table" panel — aggregates the active session's workflows
 * and background tasks in one place.
 *
 * - Workflows: the live active workflow (steps + completion) plus saved
 *   workflows on disk, with Save / Load / Delete / Clear actions.
 * - Background: the task roster with a Stop button per running task.
 *
 * All mutations go through the existing slash-command endpoint
 * (`/commands/workflow`, `/commands/bg`); after each action we refetch the
 * JSON view so the panel reflects reality without parsing command text.
 */
export default function RunTablePanel({
  profile,
  pid,
  sid,
}: {
  profile: TenantProfile;
  pid: string;
  sid: string | null;
}) {
  const [wf, setWf] = useState<WorkflowsOut>({ active: null, saved: [] });
  const [bg, setBg] = useState<BackgroundTaskOut[]>([]);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    if (!sid) return;
    setLoading(true);
    setErr(null);
    try {
      const [w, b] = await Promise.all([
        getRunTableWorkflows(profile, pid, sid),
        getRunTableBackground(profile, pid, sid),
      ]);
      setWf(w);
      setBg(b);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [profile, pid, sid]);

  // Load on mount + whenever the active session changes. Poll every 5s
  // so background-task status ticks without manual refresh.
  useEffect(() => {
    void refresh();
    if (!sid) return;
    const t = setInterval(() => void refresh(), 5000);
    return () => clearInterval(t);
  }, [refresh, sid]);

  async function act(label: string, name: string, args?: string) {
    if (!sid) return;
    setBusy(label);
    setErr(null);
    try {
      await runCommandOnce(profile, pid, sid, name, args);
      await refresh();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(null);
    }
  }

  if (!sid) {
    return <div className="text-xs text-ink-dim">no active session</div>;
  }

  return (
    <div className="space-y-3 text-sm">
      <div className="flex items-center justify-between">
        <span className="text-xs text-ink-dim uppercase tracking-wide">run table</span>
        <button
          onClick={() => void refresh()}
          disabled={loading}
          className="text-xs px-2 py-0.5 rounded border border-border hover:border-accent disabled:opacity-50"
        >
          {loading ? "…" : "↻"}
        </button>
      </div>

      {err && (
        <div className="text-xs text-err bg-err/10 border border-err/40 rounded px-2 py-1">
          {err}
        </div>
      )}

      {/* Workflows */}
      <section className="space-y-1.5">
        <div className="flex items-center justify-between">
          <span className="text-xs font-medium text-ink">workflows</span>
          <button
            onClick={() => void act("save", "workflow", "save")}
            disabled={busy !== null || !wf.active}
            className="text-xs px-1.5 py-0.5 rounded border border-border hover:border-accent disabled:opacity-40"
            title="save active workflow to disk"
          >
            {busy === "save" ? "…" : "save"}
          </button>
        </div>

        {wf.active ? (
          <ActiveWorkflow wf={wf.active} />
        ) : (
          <div className="text-xs text-ink-faint italic">no active workflow</div>
        )}

        {wf.saved.length > 0 && (
          <div className="space-y-1">
            <div className="text-xs text-ink-dim pt-1">saved</div>
            {wf.saved.map((s) => (
              <div
                key={s.id}
                className="flex items-center gap-1 text-xs bg-bg-card border border-border rounded px-2 py-1"
              >
                <span className="text-ink truncate flex-1">{s.name}</span>
                <span className="text-ink-faint">{(s.results && Object.keys(s.results).length) ?? 0}/{s.steps?.length ?? 0}</span>
                <button
                  onClick={() => void act(`load:${s.id}`, "workflow", `load ${s.id}`)}
                  disabled={busy !== null}
                  className="px-1 hover:text-accent disabled:opacity-40"
                  title="load"
                >
                  ⬆
                </button>
                <button
                  onClick={() => void act(`del:${s.id}`, "workflow", `delete ${s.id}`)}
                  disabled={busy !== null}
                  className="px-1 hover:text-err disabled:opacity-40"
                  title="delete"
                >
                  ✕
                </button>
              </div>
            ))}
          </div>
        )}

        {wf.active && (
          <button
            onClick={() => void act("clear", "workflow", "clear")}
            disabled={busy !== null}
            className="text-xs text-ink-dim hover:text-err disabled:opacity-40"
          >
            clear active
          </button>
        )}
      </section>

      {/* Background tasks */}
      <section className="space-y-1.5">
        <span className="text-xs font-medium text-ink">background</span>
        {bg.length === 0 ? (
          <div className="text-xs text-ink-faint italic">no background tasks</div>
        ) : (
          bg.map((t) => (
            <div
              key={t.bg_id}
              className="flex items-center gap-1 text-xs bg-bg-card border border-border rounded px-2 py-1"
            >
              <span>
                {t.status === "running" ? "🟢" : t.status === "completed" ? "✅" : t.status === "stopped" ? "⛔" : "❓"}
              </span>
              <span className="text-ink truncate flex-1">{t.command ?? t.tool}</span>
              {t.status === "running" && (
                <button
                  onClick={() => void act(`stop:${t.bg_id}`, "bg", `stop ${t.bg_id}`)}
                  disabled={busy !== null}
                  className="px-1 hover:text-err disabled:opacity-40"
                  title="stop"
                >
                  ⏹
                </button>
              )}
            </div>
          ))
        )}
      </section>
    </div>
  );
}

function ActiveWorkflow({ wf }: { wf: NonNullable<WorkflowsOut["active"]> }) {
  const done = wf.results ? Object.keys(wf.results).length : 0;
  const total = wf.steps?.length ?? 0;
  return (
    <div className="text-xs bg-bg-card border border-border rounded p-2 space-y-1">
      <div className="flex items-center gap-2">
        <span className="text-ink font-medium truncate">{wf.name}</span>
        <span className="text-ink-faint">{wf.status}</span>
      </div>
      <div className="text-ink-faint">{done}/{total} steps done</div>
      {wf.steps && wf.steps.length > 0 && (
        <ul className="space-y-0.5">
          {wf.steps.map((s) => {
            const isDone = wf.results ? s.id in wf.results : false;
            return (
              <li key={s.id} className="flex items-center gap-1">
                <span>{isDone ? "✅" : "◻️"}</span>
                <span className="text-ink-dim">{s.id}</span>
                {s.condition && (
                  <span className="text-ink-faint">if {s.condition}</span>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
