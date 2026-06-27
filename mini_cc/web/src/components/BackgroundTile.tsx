/**
 * F4.2: inline background-task progress tile.
 *
 * Renders inside an expanded tool activity when the activity result
 * contains "[Background task bg_xxx started]". Polls the single-task
 * status endpoint every 2s while the task is running; stops when the
 * task completes or 404s (drained).
 */
import { useEffect, useRef, useState } from "react";
import { getOneBackground, type BackgroundTaskOut } from "../lib/api";
import type { TenantProfile } from "../lib/types";

interface Props {
  profile: TenantProfile;
  pid: string;
  sid: string;
  bgId: string;
  /** Called when the task finishes — parent can stop polling. */
  onDone?: (result: string) => void;
}

const POLL_MS = 2000;

export default function BackgroundTile({ profile, pid, sid, bgId, onDone }: Props) {
  const [task, setTask] = useState<BackgroundTaskOut | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [collapsed, setCollapsed] = useState(false);
  const doneRef = useRef(false);

  useEffect(() => {
    let stop = false;
    async function poll() {
      while (!stop && !doneRef.current) {
        try {
          const t = await getOneBackground(profile, pid, sid, bgId);
          if (stop) return;
          if (t === null) {
            // Task was drained as a task_notification — parent activity
            // will receive the result via the next turn. Just stop polling.
            doneRef.current = true;
            setError("Task completed and was reported in a later turn.");
            return;
          }
          setTask(t);
          if (t.status !== "running") {
            doneRef.current = true;
            onDone?.(t.result ?? "");
            return;
          }
        } catch (e) {
          if (stop) return;
          setError(String(e));
          return;
        }
        await new Promise(r => setTimeout(r, POLL_MS));
      }
    }
    void poll();
    return () => { stop = true; };
  }, [profile, pid, sid, bgId, onDone]);

  const status = task?.status ?? "fetching…";
  const isRunning = status === "running";

  return (
    <div className="border border-border rounded-md bg-bg-card overflow-hidden">
      <div className="flex items-center gap-2 px-3 py-2 bg-bg-hover">
        <span className={`size-2 rounded-full ${
          isRunning ? "bg-warn animate-pulse" :
          status === "completed" ? "bg-ok" :
          status === "stopped" ? "bg-err" : "bg-ink-dim"
        }`} />
        <span className="text-sm font-mono text-accent">{bgId}</span>
        <span className="text-xs text-ink-dim">{status}</span>
        {task?.result && (
          <button onClick={() => setCollapsed(v => !v)}
            className="ml-auto text-xs text-accent hover:underline">
            {collapsed ? "Show output" : "Hide output"}
          </button>
        )}
      </div>
      {!collapsed && task?.result && (
        <pre className="text-xs font-mono whitespace-pre-wrap break-all p-2 max-h-64 overflow-auto bg-bg">
          {task.result}
        </pre>
      )}
      {error && (
        <div className="text-xs text-ink-dim italic px-3 py-2">{error}</div>
      )}
    </div>
  );
}
