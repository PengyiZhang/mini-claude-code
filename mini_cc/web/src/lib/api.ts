import type {
  ApiErrorEnvelope,
  FileContent,
  KeyOut,
  MetricSnapshot,
  PermissionRequestOut,
  ProjectOut,
  RotateKeyOut,
  SessionMeta,
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
