export interface TenantProfile {
  apiKey: string;
  tenantId: string;
  label: string;
  baseUrl: string;
}

export interface ProjectOut {
  project_id: string;
  tenant_id: string;
  display_name: string;
  created_at: string;
}

export interface TreeNode {
  name: string;
  path: string;
  is_dir: boolean;
  size: number;
  modified: number;
}

export interface FileContent {
  path: string;
  name: string;
  size: number;
  modified: number;
  is_text: boolean;
  truncated: boolean;
  content: string | null;
}

export interface ApiErrorEnvelope {
  error: {
    code: string;
    message: string;
    details: Record<string, unknown>;
  };
}

// ── Admin / keys (Phase F) ───────────────────────────────────────────

export interface KeyOut {
  key: string;
  tenant_id: string;
  scopes: string[];
  created_at: string;
  expires_at: string | null;
  label: string;
  rotated_from: string | null;
}

export interface RotateKeyOut {
  new_key: KeyOut;
  old_key: KeyOut | null;
}

// ── Sessions ─────────────────────────────────────────────────────────

export interface SessionMeta {
  session_id: string;
  created_at: string;
  last_active_at: string;
  message_count: number;
  in_memory: boolean;
}

// ── Permissions ──────────────────────────────────────────────────────

export interface PermissionRequestOut {
  request_id: string;
  session_id: string;
  tool_name: string;
  tool_input: Record<string, unknown>;
  created_at: string;
}

// ── Metrics ──────────────────────────────────────────────────────────

export interface MetricSeries {
  labels: Record<string, string>;
  value?: number;
  bucket_counts?: number[];
  sum?: number;
  count?: number;
}

export interface CounterFamily {
  help: string;
  label_names: string[];
  series: MetricSeries[];
}

export interface HistogramFamily {
  help: string;
  label_names: string[];
  buckets: number[];
  series: MetricSeries[];
}

export interface GaugeFamily {
  help: string;
  value: number;
}

export interface MetricSnapshot {
  scrape_ts: string;
  counters: Record<string, CounterFamily>;
  histograms: Record<string, HistogramFamily>;
  gauges: Record<string, GaugeFamily>;
}

// ── SSE ──────────────────────────────────────────────────────────────

export interface TodoItem {
  content: string;
  status: "pending" | "in_progress" | "completed";
}

export type SendEvent =
  | { type: "text"; text: string }
  | { type: "tool_use"; name: string; input: Record<string, unknown>; id: string }
  | { type: "tool_result"; tool_use_id: string; content: string }
  | { type: "permission_request"; request_id: string; tool_name: string; tool_input: Record<string, unknown>; ttl_seconds?: number }
  | { type: "permission_resolved"; request_id: string; decision: "allow" | "deny" }
  | { type: "session_warm"; session_id: string }
  | { type: "session_resumed"; project_id: string; session_id: string }
  | { type: "todos_updated"; todos: TodoItem[] }
  | { type: "done" }
  | { type: "error"; message: string }
  | { type: "max_tokens_escalation"; max_tokens: number }
  | { type: "cron_fired"; job_id: string; prompt: string }
  | { type: "background_notification" };
