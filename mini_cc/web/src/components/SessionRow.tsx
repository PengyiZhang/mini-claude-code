import { useState } from "react";
import { ApiError, resumeSession } from "../lib/api";
import type { TenantProfile } from "../lib/types";

export default function SessionRow({
  sid,
  profile,
  pid,
  active,
  inMemory,
  onClick,
  onRemove,
}: {
  sid: string;
  profile: TenantProfile;
  pid: string;
  active: boolean;
  inMemory: boolean;
  onClick: () => void;
  onRemove: () => void;
}) {
  const [warming, setWarming] = useState(false);
  const [warmed, setWarmed] = useState(inMemory);
  const [err, setErr] = useState<string | null>(null);

  async function warm() {
    setWarming(true);
    setErr(null);
    try {
      await resumeSession(profile, pid, sid);
      setWarmed(true);
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : (e as Error).message);
    } finally {
      setWarming(false);
    }
  }

  return (
    <div className="space-y-0.5">
      <div
        className={`text-sm font-mono px-2 py-1 rounded cursor-pointer flex justify-between items-center group ${
          active ? "bg-bg-hover text-ink" : "text-ink-dim hover:bg-bg-hover"
        }`}
        onClick={onClick}
      >
        <span className="flex items-center gap-2 truncate flex-1">
          <span
            title={warmed ? "warm (in memory)" : "cold (on disk only)"}
            className={`size-2 rounded-full ${warmed ? "bg-emerald-500" : "bg-slate-500"}`}
          />
          <span className="truncate">{sid}</span>
        </span>
        <span className="flex items-center gap-1">
          {!warmed && (
            <button
              onClick={(e) => {
                e.stopPropagation();
                warm();
              }}
              disabled={warming}
              title="warm up this session (load into memory)"
              className="opacity-0 group-hover:opacity-100 text-xs px-1.5 py-0.5 border border-border rounded hover:border-accent disabled:opacity-50"
            >
              {warming ? "…" : "warm"}
            </button>
          )}
          <button
            onClick={(e) => {
              e.stopPropagation();
              onRemove();
            }}
            className="opacity-0 group-hover:opacity-100 text-err/70 hover:text-err text-xs"
          >
            ×
          </button>
        </span>
      </div>
      {err && (
        <div className="ml-6 text-xs text-err bg-err/10 border border-err/40 rounded px-2 py-0.5">
          {err}
        </div>
      )}
    </div>
  );
}
