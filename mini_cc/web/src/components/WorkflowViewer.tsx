import { useMemo, useState } from "react";
import type { WorkflowOut } from "../lib/api";
import { workflowToMermaid, workflowToYAML } from "../lib/workflow_render";
import { MermaidRenderer } from "./MermaidRenderer";

type Tab = "mermaid" | "json" | "yaml";

/**
 * Workflow visualization — 3 tabs:
 *   - Mermaid flowchart (steps, parallel_with forks, condition labels,
 *     done/active node styling)
 *   - JSON tree (pretty-printed full workflow dict)
 *   - YAML (compact, single-quoted scalar rendering)
 *
 * Rendered inside a slide-out drawer; caller controls open/close via
 * the `wf` prop being non-null.
 */
export function WorkflowViewer({
  wf,
  onClose,
}: {
  wf: WorkflowOut | null;
  onClose: () => void;
}) {
  const [tab, setTab] = useState<Tab>("mermaid");

  const mermaid = useMemo(() => (wf ? workflowToMermaid(wf) : ""), [wf]);
  const yaml = useMemo(() => (wf ? workflowToYAML(wf) : ""), [wf]);
  const json = useMemo(() => (wf ? JSON.stringify(wf, null, 2) : ""), [wf]);

  if (!wf) return null;

  const tabs: { id: Tab; label: string }[] = [
    { id: "mermaid", label: "mermaid" },
    { id: "json", label: "json" },
    { id: "yaml", label: "yaml" },
  ];

  return (
    <div
      className="fixed inset-0 z-50 flex justify-end bg-black/40"
      onClick={onClose}
    >
      <div
        className="w-full max-w-3xl h-full bg-bg border-l border-border flex flex-col"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-center justify-between px-4 py-2 border-b border-border">
          <div className="min-w-0">
            <div className="text-sm font-medium text-ink truncate">
              {wf.name}
            </div>
            <div className="text-xs text-ink-faint">
              {wf.id}
              {wf.status ? ` · ${wf.status}` : ""}
              {wf.current_step ? ` · → ${wf.current_step}` : ""}
            </div>
          </div>
          <button
            onClick={onClose}
            className="text-ink-dim hover:text-ink text-lg leading-none px-2"
            title="close"
          >
            ✕
          </button>
        </div>

        {/* Tab strip */}
        <div className="flex border-b border-border bg-bg-card">
          {tabs.map((t) => (
            <button
              key={t.id}
              onClick={() => setTab(t.id)}
              className={
                "px-4 py-2 text-xs font-mono " +
                (tab === t.id
                  ? "text-ink border-b-2 border-accent bg-bg"
                  : "text-ink-dim hover:text-ink")
              }
            >
              {t.label}
            </button>
          ))}
        </div>

        {/* Body */}
        <div className="flex-1 overflow-auto p-4">
          {tab === "mermaid" && (
            <div className="flex justify-center">
              <MermaidRenderer chart={mermaid} />
            </div>
          )}
          {tab === "json" && (
            <pre className="text-xs font-mono text-ink whitespace-pre-wrap break-words">
              {json}
            </pre>
          )}
          {tab === "yaml" && (
            <pre className="text-xs font-mono text-ink whitespace-pre-wrap break-words">
              {yaml}
            </pre>
          )}
        </div>

        {/* Footer — copy current view */}
        <div className="border-t border-border px-4 py-2 flex justify-end">
          <button
            onClick={() => {
              const text = tab === "yaml" ? yaml : tab === "json" ? json : mermaid;
              void navigator.clipboard?.writeText(text);
            }}
            className="text-xs px-2 py-1 rounded border border-border hover:border-accent text-ink-dim"
          >
            copy {tab}
          </button>
        </div>
      </div>
    </div>
  );
}
