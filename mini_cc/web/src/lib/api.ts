import type {
  ApiErrorEnvelope,
  ChannelOut,
  FileContent,
  KeyOut,
  MetricSnapshot,
  PermissionRequestOut,
  ProjectOut,
  RotateKeyOut,
  SessionMeta,
  TeamEvent,
  TenantProfile,
  TodoItem,
  TreeNode,
} from "./types";

const DEFAULT_BASE = "http://127.0.0.1:8002";

export class ApiError extends Error {
  code: string;
  status: number;
  details: Record<string, unknown>;
  constructor(status: number, code: string, message: string, details: Record<string, unknown> = {}) {
    super(message);
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

export async function parseErr(res: Response): Promise<never> {
  let body: ApiErrorEnvelope | null = null;
  try {
    body = (await res.json()) as ApiErrorEnvelope;
  } catch {
    /* ignore */
  }
  const code = body?.error?.code ?? "unknown";
  const msg = body?.error?.message ?? `HTTP ${res.status}`;
  const details = body?.error?.details ?? {};
  throw new ApiError(res.status, code, msg, details);
}

export function authHeaders(profile: TenantProfile): Record<string, string> {
  return {
    Authorization: `Bearer ${profile.apiKey}`,
  };
}

export function tenantPath(profile: TenantProfile, suffix = ""): string {
  return `${profile.baseUrl}/tenants/${profile.tenantId}${suffix}`;
}

export async function verifyKey(
  baseUrl: string,
  apiKey: string,
  tenantId: string,
): Promise<ProjectOut[]> {
  // Probe by listing projects under the supplied tenant. 200 → ok.
  // 401 → unknown key. 403 → valid key but wrong tenant.
  const res = await fetch(`${baseUrl}/tenants/${tenantId}/projects`, {
    headers: { Authorization: `Bearer ${apiKey}` },
  });
  if (res.status === 401) {
    throw new ApiError(401, "unauthorized", "unknown API key");
  }
  if (res.status === 403) {
    throw new ApiError(403, "forbidden", "api key does not match tenant");
  }
  if (!res.ok) await parseErr(res);
  return res.json();
}

export async function listProjects(profile: TenantProfile): Promise<ProjectOut[]> {
  const res = await fetch(tenantPath(profile, "/projects"), {
    headers: authHeaders(profile),
  });
  if (!res.ok) await parseErr(res);
  return res.json();
}

export async function createProject(
  profile: TenantProfile,
  body: { project_id?: string; display_name?: string },
): Promise<ProjectOut> {
  const res = await fetch(tenantPath(profile, "/projects"), {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders(profile) },
    body: JSON.stringify(body),
  });
  if (!res.ok) await parseErr(res);
  return res.json();
}

export async function deleteProject(profile: TenantProfile, pid: string): Promise<void> {
  const res = await fetch(tenantPath(profile, `/projects/${pid}`), {
    method: "DELETE",
    headers: authHeaders(profile),
  });
  if (!res.ok && res.status !== 204) await parseErr(res);
}

export async function listSessionMetas(
  profile: TenantProfile,
  pid: string,
): Promise<SessionMeta[]> {
  // Sessions list endpoint accepts a query to return full meta. We use the
  // bare-id list and then call resume-sessions for cold ones. To keep one
  // round-trip, prefer /sessions when it returns SessionMeta — but the
  // current shape is string[]. Reuse string list + warm-up pings only on
  // demand (handled by the UI).
  const res = await fetch(tenantPath(profile, `/projects/${pid}/sessions`), {
    headers: authHeaders(profile),
  });
  if (!res.ok) await parseErr(res);
  const raw = (await res.json()) as unknown;
  if (Array.isArray(raw) && raw.length > 0 && typeof raw[0] === "object") {
    return raw as SessionMeta[];
  }
  return (raw as string[]).map((s) => ({
    session_id: s,
    created_at: "",
    last_active_at: "",
    message_count: 0,
    in_memory: false,
  }));
}

export async function startSession(
  profile: TenantProfile,
  pid: string,
  body: { session_id?: string; model?: string },
): Promise<{ project_id: string; session_id: string; created: boolean }> {
  const res = await fetch(tenantPath(profile, `/projects/${pid}/sessions`), {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders(profile) },
    body: JSON.stringify(body),
  });
  if (!res.ok) await parseErr(res);
  return res.json();
}

export async function resumeSession(
  profile: TenantProfile,
  pid: string,
  sid: string,
): Promise<SessionMeta> {
  const res = await fetch(
    tenantPath(profile, `/projects/${pid}/sessions/${sid}/resume`),
    { method: "POST", headers: authHeaders(profile) },
  );
  if (!res.ok) await parseErr(res);
  return res.json();
}

export async function deleteSession(profile: TenantProfile, pid: string, sid: string): Promise<void> {
  const res = await fetch(tenantPath(profile, `/projects/${pid}/sessions/${sid}`), {
    method: "DELETE",
    headers: authHeaders(profile),
  });
  if (!res.ok && res.status !== 204) await parseErr(res);
}

export interface RawMessage {
  role: "user" | "assistant";
  content: string | Array<{
    type: string;
    text?: string;
    id?: string;
    name?: string;
    input?: Record<string, unknown>;
    tool_use_id?: string;
    content?: string | unknown;
    is_error?: boolean;
  }>;
}

// Fetch the persisted transcript for a session. Used to rehydrate the
// in-memory chat state after a page reload. Without this, the chat
// history is lost on refresh even though the backend still has it on
// disk — confusing the user into thinking sessions aren't persisted.
export async function getSessionMessages(
  profile: TenantProfile,
  pid: string,
  sid: string,
): Promise<RawMessage[]> {
  const res = await fetch(
    tenantPath(profile, `/projects/${pid}/sessions/${sid}/messages`),
    { headers: authHeaders(profile) },
  );
  if (!res.ok) await parseErr(res);
  return res.json();
}

// Fetch persisted todos for the task board. Hydrates the panel after a
// page reload so the user sees the last known state without waiting for
// the model to call todo_write again.
export async function getSessionTodos(
  profile: TenantProfile,
  pid: string,
  sid: string,
): Promise<TodoItem[]> {
  const res = await fetch(
    tenantPath(profile, `/projects/${pid}/sessions/${sid}/todos`),
    { headers: authHeaders(profile) },
  );
  if (!res.ok) await parseErr(res);
  return res.json();
}

// ── Permissions ──────────────────────────────────────────────────────

export async function listPendingPermissions(
  profile: TenantProfile,
  pid: string,
  sid: string,
): Promise<PermissionRequestOut[]> {
  const res = await fetch(
    tenantPath(profile, `/projects/${pid}/sessions/${sid}/permissions`),
    { headers: authHeaders(profile) },
  );
  if (!res.ok) await parseErr(res);
  return res.json();
}

export async function decidePermission(
  profile: TenantProfile,
  pid: string,
  sid: string,
  reqId: string,
  decision: "allow" | "deny",
  message?: string,
): Promise<void> {
  const res = await fetch(
    tenantPath(profile, `/projects/${pid}/sessions/${sid}/permissions/${reqId}/decide`),
    {
      method: "POST",
      headers: { "Content-Type": "application/json", ...authHeaders(profile) },
      body: JSON.stringify({ decision, message: message ?? null }),
    },
  );
  if (!res.ok && res.status !== 204) await parseErr(res);
}

export function filesBase(profile: TenantProfile, pid: string): string {
  return tenantPath(profile, `/projects/${pid}/files`);
}

export async function listTree(profile: TenantProfile, pid: string, path = ""): Promise<TreeNode[]> {
  const url = `${filesBase(profile, pid)}/tree?path=${encodeURIComponent(path)}`;
  const res = await fetch(url, { headers: authHeaders(profile) });
  if (!res.ok) await parseErr(res);
  return res.json();
}

export async function readContent(
  profile: TenantProfile,
  pid: string,
  path: string,
): Promise<FileContent> {
  const url = `${filesBase(profile, pid)}/content?path=${encodeURIComponent(path)}`;
  const res = await fetch(url, { headers: authHeaders(profile) });
  if (!res.ok) await parseErr(res);
  return res.json();
}

export async function mkdir(profile: TenantProfile, pid: string, path: string): Promise<void> {
  const res = await fetch(`${filesBase(profile, pid)}/mkdir?path=${encodeURIComponent(path)}`, {
    method: "POST",
    headers: authHeaders(profile),
  });
  if (!res.ok) await parseErr(res);
}

export async function uploadFiles(
  profile: TenantProfile,
  pid: string,
  dir: string,
  files: File[],
  relPaths?: string[],
): Promise<{ uploaded: number; files: { path: string; size: number }[] }> {
  const fd = new FormData();
  for (const f of files) fd.append("files", f);
  if (relPaths) {
    for (const r of relPaths) fd.append("rel_paths", r);
  }
  const res = await fetch(
    `${filesBase(profile, pid)}/upload?path=${encodeURIComponent(dir)}`,
    { method: "POST", headers: authHeaders(profile), body: fd },
  );
  if (!res.ok) await parseErr(res);
  return res.json();
}

export async function deletePath(profile: TenantProfile, pid: string, path: string): Promise<void> {
  const res = await fetch(`${filesBase(profile, pid)}?path=${encodeURIComponent(path)}`, {
    method: "DELETE",
    headers: authHeaders(profile),
  });
  if (!res.ok) await parseErr(res);
}

export async function downloadZip(profile: TenantProfile, pid: string): Promise<Blob> {
  const res = await fetch(tenantPath(profile, `/projects/${pid}/download`), {
    headers: authHeaders(profile),
  });
  if (!res.ok) await parseErr(res);
  return res.blob();
}

// ── Run Table (Phase J) ──────────────────────────────────────────────
// Read-only JSON views backing the sidebar "Run Table" tab. Both are
// session-scoped because background tasks + the active workflow live on
// the session's loop at runtime.

export interface WorkflowStepOut {
  id: string;
  prompt: string;
  condition?: string | null;
  parallel_with?: string | null;
  on_failure?: string;
  max_retries?: number;
}

export interface WorkflowOut {
  id: string;
  name: string;
  description?: string;
  status?: string;
  current_step?: string | null;
  steps?: WorkflowStepOut[];
  results?: Record<string, string>;
  state?: Record<string, unknown>;
  saved_at?: string;
}

export interface WorkflowsOut {
  active: WorkflowOut | null;
  saved: WorkflowOut[];
}

export interface BackgroundTaskOut {
  bg_id: string;
  tool: string;
  command?: string;
  status: string;
  result?: string;
}

export async function getRunTableWorkflows(
  profile: TenantProfile,
  pid: string,
  sid: string,
): Promise<WorkflowsOut> {
  const res = await fetch(
    tenantPath(profile, `/projects/${pid}/sessions/${sid}/run-table/workflows`),
    { headers: authHeaders(profile) },
  );
  if (!res.ok) await parseErr(res);
  return (await res.json()) as WorkflowsOut;
}

export async function getRunTableBackground(
  profile: TenantProfile,
  pid: string,
  sid: string,
): Promise<BackgroundTaskOut[]> {
  const res = await fetch(
    tenantPath(profile, `/projects/${pid}/sessions/${sid}/run-table/background`),
    { headers: authHeaders(profile) },
  );
  if (!res.ok) await parseErr(res);
  return (await res.json()) as BackgroundTaskOut[];
}

// F4.2: single-task status for the inline background tile. Returns
// null when the task has been drained (404) so the caller can render
// the original tool_use result instead.
export async function getOneBackground(
  profile: TenantProfile,
  pid: string,
  sid: string,
  bgId: string,
): Promise<BackgroundTaskOut | null> {
  const res = await fetch(
    tenantPath(profile, `/projects/${pid}/sessions/${sid}/run-table/background/${encodeURIComponent(bgId)}`),
    { headers: authHeaders(profile) },
  );
  if (res.status === 404) return null;
  if (!res.ok) await parseErr(res);
  return (await res.json()) as BackgroundTaskOut;
}

// ── Admin / keys (Phase F) ───────────────────────────────────────────
// Admin uses an explicit tenantId (may differ from chat tenant profile).

export interface AdminProfile {
  baseUrl: string;
  tenantId: string;
  apiKey: string;
}

function adminPath(p: AdminProfile, suffix: string): string {
  return `${p.baseUrl}/tenants/${p.tenantId}/admin${suffix}`;
}

function adminHeaders(p: AdminProfile): Record<string, string> {
  return { Authorization: `Bearer ${p.apiKey}` };
}

/** Probe by listing keys — 200 means the key has admin:read. */
export async function verifyAdmin(p: AdminProfile): Promise<KeyOut[]> {
  const res = await fetch(adminPath(p, "/keys"), { headers: adminHeaders(p) });
  if (res.status === 401 || res.status === 403) {
    throw new ApiError(res.status, "forbidden", "key lacks admin:read scope");
  }
  if (!res.ok) await parseErr(res);
  return res.json();
}

export async function adminListKeys(p: AdminProfile): Promise<KeyOut[]> {
  const res = await fetch(adminPath(p, "/keys"), { headers: adminHeaders(p) });
  if (!res.ok) await parseErr(res);
  return res.json();
}

export async function adminCreateKey(
  p: AdminProfile,
  body: { scopes?: string[]; expires_in?: string; label?: string },
): Promise<KeyOut> {
  const res = await fetch(adminPath(p, "/keys"), {
    method: "POST",
    headers: { "Content-Type": "application/json", ...adminHeaders(p) },
    body: JSON.stringify(body),
  });
  if (!res.ok) await parseErr(res);
  return res.json();
}

export async function adminUpdateKey(
  p: AdminProfile,
  key: string,
  body: { scopes?: string[]; expires_in?: string; label?: string },
): Promise<KeyOut> {
  const res = await fetch(adminPath(p, `/keys/${encodeURIComponent(key)}`), {
    method: "PATCH",
    headers: { "Content-Type": "application/json", ...adminHeaders(p) },
    body: JSON.stringify(body),
  });
  if (!res.ok) await parseErr(res);
  return res.json();
}

export async function adminRevokeKey(p: AdminProfile, key: string): Promise<void> {
  const res = await fetch(adminPath(p, `/keys/${encodeURIComponent(key)}`), {
    method: "DELETE",
    headers: adminHeaders(p),
  });
  if (!res.ok && res.status !== 204) await parseErr(res);
}

export async function adminRotateKey(
  p: AdminProfile,
  key: string,
  body: { grace_hours?: number; scopes?: string[]; expires_in?: string; label?: string },
): Promise<RotateKeyOut> {
  const res = await fetch(adminPath(p, `/keys/${encodeURIComponent(key)}/rotate`), {
    method: "POST",
    headers: { "Content-Type": "application/json", ...adminHeaders(p) },
    body: JSON.stringify(body),
  });
  if (!res.ok) await parseErr(res);
  return res.json();
}

export async function adminMetrics(p: AdminProfile): Promise<MetricSnapshot> {
  const res = await fetch(adminPath(p, "/metrics.json"), { headers: adminHeaders(p) });
  if (!res.ok) await parseErr(res);
  return res.json();
}

export { DEFAULT_BASE };

// ── Workflow V2 (W1-W6) ─────────────────────────────────────────────────

import type {
  WorkflowV2Definition,
  WorkflowV2Run,
  WorkflowV2Step,
} from "./types";

export type WorkflowV2DefinitionOut = WorkflowV2Definition;

export interface CreateWorkflowV2DefIn {
  name: string;
  description?: string;
  steps: WorkflowV2Step[];
  triggers?: { type: string; config: Record<string, unknown> }[];
  state_schema?: Record<string, unknown>;
}

function wfDefPath(profile: TenantProfile, pid: string, suffix = ""): string {
  return `${profile.baseUrl}/tenants/${profile.tenantId}/projects/${pid}/workflow-definitions${suffix}`;
}

function wfRunPath(profile: TenantProfile, pid: string, suffix = ""): string {
  return `${profile.baseUrl}/tenants/${profile.tenantId}/projects/${pid}/workflow-runs${suffix}`;
}

export async function listWorkflowV2Defs(
  profile: TenantProfile,
  pid: string,
): Promise<WorkflowV2Definition[]> {
  const res = await fetch(wfDefPath(profile, pid), { headers: authHeaders(profile) });
  if (!res.ok) await parseErr(res);
  return res.json();
}

export async function createWorkflowV2Def(
  profile: TenantProfile,
  pid: string,
  body: CreateWorkflowV2DefIn,
): Promise<WorkflowV2Definition> {
  const res = await fetch(wfDefPath(profile, pid), {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders(profile) },
    body: JSON.stringify(body),
  });
  if (!res.ok) await parseErr(res);
  return res.json();
}

export async function updateWorkflowV2Def(
  profile: TenantProfile,
  pid: string,
  defId: string,
  body: Partial<CreateWorkflowV2DefIn>,
): Promise<WorkflowV2Definition> {
  const res = await fetch(wfDefPath(profile, pid, `/${defId}`), {
    method: "PUT",
    headers: { "Content-Type": "application/json", ...authHeaders(profile) },
    body: JSON.stringify(body),
  });
  if (!res.ok) await parseErr(res);
  return res.json();
}

export async function deleteWorkflowV2Def(
  profile: TenantProfile,
  pid: string,
  defId: string,
): Promise<void> {
  const res = await fetch(wfDefPath(profile, pid, `/${defId}`), {
    method: "DELETE",
    headers: authHeaders(profile),
  });
  if (!res.ok && res.status !== 204) await parseErr(res);
}

export async function startWorkflowV2Run(
  profile: TenantProfile,
  pid: string,
  defId: string,
  body: { initial_state?: Record<string, unknown>; trigger?: Record<string, unknown> } = {},
): Promise<WorkflowV2Run> {
  const res = await fetch(wfDefPath(profile, pid, `/${defId}/runs`), {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders(profile) },
    body: JSON.stringify(body),
  });
  if (!res.ok) await parseErr(res);
  return res.json();
}

export async function listWorkflowV2Runs(
  profile: TenantProfile,
  pid: string,
  defId?: string,
): Promise<WorkflowV2Run[]> {
  const url = defId
    ? wfRunPath(profile, pid, `?def_id=${encodeURIComponent(defId)}`)
    : wfRunPath(profile, pid);
  const res = await fetch(url, { headers: authHeaders(profile) });
  if (!res.ok) await parseErr(res);
  return res.json();
}

export async function getWorkflowV2Run(
  profile: TenantProfile,
  pid: string,
  runId: string,
): Promise<WorkflowV2Run> {
  const res = await fetch(wfRunPath(profile, pid, `/${runId}`), {
    headers: authHeaders(profile),
  });
  if (!res.ok) await parseErr(res);
  return res.json();
}

export async function cancelWorkflowV2Run(
  profile: TenantProfile,
  pid: string,
  runId: string,
): Promise<WorkflowV2Run> {
  const res = await fetch(wfRunPath(profile, pid, `/${runId}`), {
    method: "DELETE",
    headers: authHeaders(profile),
  });
  if (!res.ok) await parseErr(res);
  return res.json();
}

export async function driveWorkflowV2Run(
  profile: TenantProfile,
  pid: string,
  runId: string,
  sessionId: string,
): Promise<WorkflowV2Run> {
  const res = await fetch(wfRunPath(profile, pid, `/${runId}/drive`), {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders(profile) },
    body: JSON.stringify({ session_id: sessionId }),
  });
  if (!res.ok) await parseErr(res);
  return res.json();
}

export async function resolveWorkflowV2Gate(
  profile: TenantProfile,
  pid: string,
  runId: string,
  stepId: string,
  body: { approver?: string; decision: "approve" | "reject"; feedback?: string },
): Promise<WorkflowV2Run> {
  const res = await fetch(wfRunPath(profile, pid, `/${runId}/steps/${stepId}/resolve`), {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders(profile) },
    body: JSON.stringify(body),
  });
  if (!res.ok) await parseErr(res);
  return res.json();
}

// ── Team Activity (Phase I.C.2) ──────────────────────────────────────
// Aggregated team event feed (I.C.1 backend endpoint). since/limit/teammate
// are optional query params. Returns merged events sorted ascending by ts
// alongside a has_more cursor flag.

export async function fetchTeamActivity(
  profile: TenantProfile,
  pid: string,
  opts?: { since?: string; limit?: number; teammate?: string },
): Promise<{ events: TeamEvent[]; has_more: boolean }> {
  const params = new URLSearchParams();
  if (opts?.since) params.set("since", opts.since);
  if (opts?.limit) params.set("limit", String(opts.limit));
  if (opts?.teammate) params.set("teammate", opts.teammate);
  const qs = params.toString() ? `?${params.toString()}` : "";
  const res = await fetch(
    tenantPath(profile, `/projects/${pid}/team/activity${qs}`),
    { headers: authHeaders(profile) },
  );
  if (!res.ok) await parseErr(res);
  return (await res.json()) as { events: TeamEvent[]; has_more: boolean };
}

// ── Channels (bidirectional IM transport) ────────────────────────────
// Project-scoped CRUD against /tenants/{tid}/projects/{pid}/channels.
// The inbound webhook URL is public (no tenant auth) and constructed
// client-side from window.location.origin so the user copies the URL
// their browser sees — which is what Feishu will reach.

export function channelWebhookUrl(channelId: string): string {
  return `${window.location.origin}/channels/${channelId}/webhook`;
}

export async function listChannels(
  profile: TenantProfile,
  pid: string,
): Promise<ChannelOut[]> {
  const res = await fetch(tenantPath(profile, `/projects/${pid}/channels`), {
    headers: authHeaders(profile),
  });
  if (!res.ok) await parseErr(res);
  return res.json();
}

export async function createChannel(
  profile: TenantProfile,
  pid: string,
  body: {
    kind: string;
    config: Record<string, unknown>;
    session_id?: string | null;
    event_types?: string[];
    transport?: "ws" | "webhook";
  },
): Promise<ChannelOut> {
  const res = await fetch(tenantPath(profile, `/projects/${pid}/channels`), {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders(profile) },
    body: JSON.stringify(body),
  });
  if (!res.ok) await parseErr(res);
  return res.json();
}

export async function deleteChannel(
  profile: TenantProfile,
  pid: string,
  channelId: string,
): Promise<void> {
  const res = await fetch(
    tenantPath(profile, `/projects/${pid}/channels/${channelId}`),
    {
      method: "DELETE",
      headers: authHeaders(profile),
    },
  );
  if (!res.ok && res.status !== 204) await parseErr(res);
}
