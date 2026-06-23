import { useEffect, useState } from "react";
import { ApiError, readContent } from "../lib/api";
import { useAuth } from "../lib/store";
import type { FileContent } from "../lib/types";
import { MarkdownRenderer } from "./MarkdownRenderer"
export default function FilePreview({ pid, path }: { pid: string; path: string }) {
  const profile = useAuth((s) => s.current())!;
  const [data, setData] = useState<FileContent | null>(null);
  const [busy, setBusy] = useState(true);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setBusy(true);
    setErr(null);
    readContent(profile, pid, path)
      .then((d) => {
        if (!cancelled) setData(d);
      })
      .catch((e) => {
        if (!cancelled) setErr(e instanceof ApiError ? e.message : (e as Error).message);
      })
      .finally(() => !cancelled && setBusy(false));
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path]);

  if (busy) return <div className="text-sm text-ink-dim p-4">loading…</div>;
  if (err) return <div className="text-sm text-err p-4">{err}</div>;
  if (!data) return null;

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between text-xs text-ink-dim">
        <div className="font-mono truncate">{data.path}</div>
        <div>
          {data.is_text ? "text" : "binary"} · {data.size}B
          {data.truncated && <span className="text-warn ml-2">truncated</span>}
        </div>
      </div>
      {data.is_text && data.content !== null ? (
        <pre className="bg-gray text-white border border-border rounded p-3 overflow-auto text-xs max-h-[80vh] font-mono whitespace-pre-wrap break-all">
          {/* {data.content} */}
          <MarkdownRenderer content={data.content} />
        </pre>
      ) : (
        <div className="bg-bg border border-border rounded p-4 text-sm text-ink-dim">
          binary file — preview not available
        </div>
      )}
    </div>
  );
}
