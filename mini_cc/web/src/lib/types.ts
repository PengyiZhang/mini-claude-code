export interface TenantProfile {
  apiKey: string;
  tenantId: string;
  label: string;
  baseUrl: string;
}

// ── Workflow V2 (W1-W6) ────────────────────────────────────────────────

export type WorkflowV2StepType =
  | "action"
  | "validate"
  | "checkpoint"
  | "webhook_wait"
  | "email_wait"
  | "branch"
  | "loop";

export interface WorkflowV2Step {
  id: string;
  type: WorkflowV2StepType;
  prompt?: string;
  inputs_schema?: Record<string, unknown>;
  outputs_schema?: Record<string, unknown>;
  config?: Record<string, unknown>;
  condition?: string | null;
  /** Explicit "go to step X after this one completes". W7: enables
   * exclusive branch targets and backward jumps for hand-rolled loops. */
  next?: string | null;
}

export interface WorkflowV2Definition {
  def_id: string;
  name: string;
  description: string;
  version: number;
  owner: string | null;
  created_at: string;
  updated_at: string;
  steps: WorkflowV2Step[];
  triggers: { type: string; config: Record<string, unknown> }[];
  state_schema: Record<string, unknown>;
}

export type WorkflowV2RunStatus =
  | "pending"
  | "running"
  | "paused"
  | "completed"
  | "failed"
  | "cancelled";

export interface WorkflowV2StepRun {
  step_id: string;
  status: "pending" | "running" | "paused" | "completed" | "failed" | "skipped";
  started_at: string | null;
  completed_at: string | null;
  output: unknown;
  error: string | null;
}

export interface WorkflowV2Run {
  run_id: string;
  def_id: string;
  def_version: number;
  status: WorkflowV2RunStatus;
  current_step_idx: number;
  started_at: string | null;
  completed_at: string | null;
  state: Record<string, unknown>;
  step_runs: WorkflowV2StepRun[];
  trigger: Record<string, unknown>;
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
  | { type: "background_notification" }
  // debug.8 Task A: teammate→lead message delivered live through the
  // spawner's lead side-channel. Shape mirrors the bus message dict.
  | { type: "teammate_message"; from: string; to: string; content: string; msg_type: string; ts: number; metadata: Record<string, unknown> }
  // Phase I.C.5: watcher fired a nudge (drained teammate milestone /
  // blocker / result messages) and is about to drive a daemon-thread
  // lead turn. The frontend renders a gray notice on the streaming
  // bubble so the user can see "Alice reported a milestone → lead is
  // responding..." at a glance. Burst-collapsed: one event per drain.
  | { type: "lead_nudged"; items: { from: string; kind: string }[] }
  | { type: "card" } & CardEvent;

// ── Slash-command card schema ───────────────────────────────────────
// A CardEvent is a structured alternative to the plain {type:"text"} event
// that slash-command handlers emit. The frontend CardView component
// dispatches on `variant` to a dedicated renderer (CardList, CardKeyValue,
// …), giving each command a typed, interactive surface.
//
// The Python-side source of truth is mini_cc/commands/cards.py — keep this
// interface in sync with the dataclass fields there. Tests pin the shape.

export type CardTone = "default" | "ok" | "warn" | "err" | "accent" | "muted";
export type CardVariant = "list" | "table" | "key_value" | "steps";
export type CardStatus = "ok" | "warning" | "error";

export interface CardBadge {
  text: string;
  tone: CardTone;
}

export interface CardAction {
  label: string;
  command: string;
  tone: CardTone;
}

export interface CardListItem {
  id: string;
  title: string;
  subtitle?: string | null;
  icon?: string | null;
  badges: CardBadge[];
  meta?: string | null;
  expandable_command?: string | null;
  menu: CardAction[];
}

export interface CardListPayload {
  items: CardListItem[];
  empty_hint?: string | null;
  summary?: string | null;
  group_by?: string | null;
}

export interface CardKeyValuePair {
  k: string;
  v: string;
  mono: boolean;
  sensitive: boolean;
  badge?: CardBadge | null;
}

export interface CardKeyValuePayload {
  pairs: CardKeyValuePair[];
}

export interface CardEvent {
  id: string;
  variant: CardVariant;
  title?: string | null;
  icon?: string | null;
  status: CardStatus;
  error_message?: string | null;
  payload: CardListPayload | CardKeyValuePayload | Record<string, unknown>;
  actions: CardAction[];
  emitted_at: number;
  revision: number;
  // Live-refresh hint: when refresh_command is set, the CardView polls
  // that slash command every refresh_interval_ms and replaces this card
  // by id in-place (no new assistant bubble). Both fields absent on
  // static cards (the common case).
  refresh_command?: string | null;
  refresh_interval_ms?: number | null;
}

// ── Team Activity (Phase I.C.2) ──────────────────────────────────────
// Backend /team/activity spreads each event's payload fields alongside
// session_id and ts, so any extra fields are accessible via indexing.
// Keep this permissive to absorb new backend payload fields without a
// type round-trip.
export interface TeamEvent {
  session_id: string;
  ts: string; // ISO timestamp from backend
  type: string; // "text" | "tool_use" | "tool_result" | "send_message" | "error" | ...
  [key: string]: unknown;
}
