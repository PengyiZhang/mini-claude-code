import { useEffect, useState } from "react";
import { decidePermission } from "../lib/api";
import { ApiError } from "../lib/api";
import type { PermissionRequestOut, TenantProfile } from "../lib/types";

export interface PermissionPromptData {
  request_id: string;
  tool_name: string;
  tool_input: Record<string, unknown>;
  ttl_seconds?: number;
}

export default function PermissionPrompt({
  profile,
  pid,
  sid,
  data,
  onResolved,
}: {
  profile: TenantProfile;
  pid: string;
  sid: string;
  data: PermissionPromptData;
  onResolved: (reqId: string, decision: "allow" | "deny") => void;
}) {
  const ttl = data.ttl_seconds ?? 0;
  const [remaining, setRemaining] = useState<number | null>(ttl > 0 ? ttl : null);
  const [busy, setBusy] = useState<"allow" | "deny" | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (remaining === null || remaining <= 0) return;
    const id = setInterval(() => {
      setRemaining((r) => (r === null ? null : Math.max(0, r - 1)));
    }, 1000);
    return () => clearInterval(id);
  }, [remaining]);

  async function decide(decision: "allow" | "deny") {
    setBusy(decision);
    setError(null);
    try {
      await decidePermission(profile, pid, sid, data.request_id, decision);
      onResolved(data.request_id, decision);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : (e as Error).message);
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="border border-amber-500/40 bg-amber-500/5 rounded p-3 space-y-2">
      <div className="flex items-center gap-2">
        <div className="size-2 rounded-full bg-amber-500 animate-pulse" />
        <div className="text-sm font-semibold">
          permission requested: <span className="font-mono">{data.tool_name}</span>
        </div>
        {remaining !== null && (
          <div className="ml-auto text-xs tabular-nums text-amber-300 font-mono">
            ⏱ {remaining}s
          </div>
        )}
      </div>
      <details className="text-xs">
        <summary className="cursor-pointer text-ink-dim">tool input</summary>
        <pre className="bg-bg border border-border rounded p-2 mt-1 overflow-auto">
{JSON.stringify(data.tool_input, null, 2)}
        </pre>
      </details>
      {error && (
        <div className="text-xs text-err bg-err/10 border border-err/40 rounded px-2 py-1">
          {error}
        </div>
      )}
      <div className="flex gap-2 justify-end">
        <button
          disabled={busy !== null}
          onClick={() => decide("deny")}
          className="text-sm px-3 py-1 border border-err/40 text-err rounded hover:bg-err/10 disabled:opacity-50"
        >
          {busy === "deny" ? "…" : "deny"}
        </button>
        <button
          disabled={busy !== null}
          onClick={() => decide("allow")}
          className="text-sm px-3 py-1 bg-emerald-600 hover:bg-emerald-700 text-white rounded disabled:opacity-50"
        >
          {busy === "allow" ? "…" : "allow"}
        </button>
      </div>
    </div>
  );
}

/** Convert SSE permission_request payload to PermissionRequestOut shape (used by pending poll). */
export function pendingToData(p: PermissionRequestOut): PermissionPromptData {
  return {
    request_id: p.request_id,
    tool_name: p.tool_name,
    tool_input: p.tool_input,
  };
}
