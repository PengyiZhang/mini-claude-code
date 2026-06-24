/**
 * Pure helpers that render a workflow into Mermaid / YAML strings.
 *
 * These are extracted from the component so they're trivially testable
 * (no React, no async) and so the backend can mirror the algorithm
 * should we ever want server-side rendering.
 */
import type { WorkflowOut } from "./api";

/**
 * Render a workflow as a Mermaid flowchart.
 *
 * - Each step is a node labeled with its id.
 * - The step's prompt (truncated) is shown as a tooltip via Mermaid's
 *   `|...|` edge-label syntax on the inbound edge from the previous step.
 * - `parallel_with` produces a fork: both branches leave the same source.
 * - `condition` is annotated on the node label.
 * - Completed steps (id present in wf.results) get a green-ish class;
 *   the active step gets a yellow class.
 */
export function workflowToMermaid(wf: WorkflowOut): string {
  const steps = wf.steps ?? [];
  if (steps.length === 0) {
    return "flowchart TD\n  empty([\"no steps\"])\n";
  }

  const lines: string[] = ["flowchart TD"];
  const results = wf.results ?? {};
  const current = wf.current_step ?? null;

  // Nodes — use step.id as the mermaid node id (sanitized).
  const safe = (s: string) => s.replace(/[^A-Za-z0-9_]/g, "_");
  for (const s of steps) {
    const nid = safe(s.id);
    const parts: string[] = [s.id];
    if (s.condition) parts.push(`if ${s.condition}`);
    if (s.parallel_with) parts.push(`∥ ${s.parallel_with}`);
    if (results[s.id] !== undefined) parts.push("✓");
    const label = parts.join(" / ").replace(/"/g, "'");
    let cls = "step";
    if (results[s.id] !== undefined) cls = "step step-done";
    if (current === s.id) cls = "step step-active";
    lines.push(`  ${nid}["${label}"]:::${cls}`);
  }

  // Edges — default: linear chain, but parallel_with forks.
  const byId = new Map(steps.map((s) => [s.id, s]));
  const linked = new Set<string>();
  for (const s of steps) {
    const nid = safe(s.id);
    if (s.parallel_with && byId.has(s.parallel_with)) {
      const src = safe(s.parallel_with);
      lines.push(`  ${src} --> ${nid}`);
      linked.add(s.id);
    }
  }
  // Anything not linked via parallel_with chains off its predecessor.
  for (let i = 0; i < steps.length; i++) {
    const s = steps[i];
    if (linked.has(s.id)) continue;
    if (i === 0) continue;
    const prev = safe(steps[i - 1].id);
    lines.push(`  ${prev} --> ${safe(s.id)}`);
  }

  // Minimal classDef styles so done/active nodes stand out without
  // requiring a CSS file change.
  lines.push("  classDef step fill:#1f2937,color:#e5e7eb,stroke:#374151");
  lines.push("  classDef step-done fill:#064e3b,color:#d1fae5,stroke:#10b981");
  lines.push("  classDef step-active fill:#78350f,color:#fde68a,stroke:#f59e0b");

  return lines.join("\n");
}

/**
 * Tiny YAML emitter — only handles the shapes a WorkflowOut can take:
 * scalars (str/int/bool/null), arrays of dicts, dicts of scalars.
 *
 * We avoid a js-yaml dep because the workflow shape is fixed and small.
 */
export function workflowToYAML(wf: WorkflowOut): string {
  const lines: string[] = [];
  const q = (v: unknown): string => {
    if (v === null || v === undefined) return "null";
    if (typeof v === "number") return String(v);
    if (typeof v === "boolean") return String(v);
    const s = String(v);
    // Quote if the value looks yaml-special (starts with a quote, colon,
    // bracket, dash, hash, or contains ": " or leading/trailing space).
    const needsQuote = /^["'#\-\[\]{}:&*!|>%@`,]/.test(s)
      || s.includes(": ")
      || s !== s.trim()
      || s === "";
    if (!needsQuote) return s;
    return `"${s.replace(/\\/g, "\\\\").replace(/"/g, '\\"')}"`;
  };

  lines.push(`id: ${q(wf.id)}`);
  lines.push(`name: ${q(wf.name)}`);
  if (wf.description) lines.push(`description: ${q(wf.description)}`);
  if (wf.status) lines.push(`status: ${q(wf.status)}`);
  if (wf.current_step) lines.push(`current_step: ${q(wf.current_step)}`);

  // State dict — scalar values only (workflow state is flat).
  const stateEntries = Object.entries(wf.state ?? {});
  if (stateEntries.length > 0) {
    lines.push("state:");
    for (const [k, v] of stateEntries) {
      lines.push(`  ${k}: ${q(v)}`);
    }
  }

  // Steps — array of dicts with fixed keys.
  if ((wf.steps ?? []).length > 0) {
    lines.push("steps:");
    for (const s of wf.steps!) {
      lines.push(`  - id: ${q(s.id)}`);
      lines.push(`    prompt: ${q(s.prompt)}`);
      if (s.condition) lines.push(`    condition: ${q(s.condition)}`);
      if (s.parallel_with) lines.push(`    parallel_with: ${q(s.parallel_with)}`);
      if (s.on_failure) lines.push(`    on_failure: ${q(s.on_failure)}`);
      if (s.max_retries && s.max_retries > 0) {
        lines.push(`    max_retries: ${s.max_retries}`);
      }
    }
  }

  // Results — step_id → string.
  const resultEntries = Object.entries(wf.results ?? {});
  if (resultEntries.length > 0) {
    lines.push("results:");
    for (const [k, v] of resultEntries) {
      lines.push(`  ${k}: ${q(v)}`);
    }
  }

  return lines.join("\n") + "\n";
}
