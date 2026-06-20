import type { ApiErrorEnvelope, FileContent, ProjectOut, TenantProfile, TreeNode } from "./types";

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

async function parseErr(res: Response): Promise<never> {
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

function authHeaders(profile: TenantProfile): Record<string, string> {
  return {
    Authorization: `Bearer ${profile.apiKey}`,
  };
}

function tenantPath(profile: TenantProfile, suffix = ""): string {
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

export async function listSessions(profile: TenantProfile, pid: string): Promise<string[]> {
  const res = await fetch(tenantPath(profile, `/projects/${pid}/sessions`), {
    headers: authHeaders(profile),
  });
  if (!res.ok) await parseErr(res);
  return res.json();
}

export async function startSession(
  profile: TenantProfile,
  pid: string,
  body: { session_id?: string; model?: string },
): Promise<{ project_id: string; session_id: string }> {
  const res = await fetch(tenantPath(profile, `/projects/${pid}/sessions`), {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders(profile) },
    body: JSON.stringify(body),
  });
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

export { DEFAULT_BASE };
