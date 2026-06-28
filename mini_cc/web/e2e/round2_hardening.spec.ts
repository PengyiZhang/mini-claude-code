/**
 * Round 2 production-hardening specs (Batch 3–7).
 *
 * Focuses on UI-visible + API-contract guarantees that the Round 2
 * changes added:
 *
 *   - tenant boundary: a def / run created under tenant A is invisible
 *     to tenant B (P0-1 in workflow_v2 router, Batch 3 tenant-scope
 *     enforcement). Round 2 audit flagged this as a cross-tenant data
 *     leak vector.
 *   - rate limiter (Batch 6): authenticated requests are subject to a
 *     per-key burst cap; over-budget calls return 429 (not 500).
 *
 * Skipped here (covered by unit tests, not e2e):
 *   - atomic storage writes / corruption detection (Batch 3) — fs-only
 *   - SSE queue bound + heartbeat + Last-Event-Id (Batch 4) — needs
 *     fault injection
 *   - hook fault isolation (Batch 6) — needs a misconfigured hook
 *   - log redaction (Batch 6) — backend-only verification
 */
import { test, expect } from "@playwright/test";
import { BASE } from "./helpers";

const E2E_KEY = process.env.E2E_API_KEY!;

/** Fetch helper that does NOT auto-fail on non-2xx — returns res. */
async function raw(request: import("@playwright/test").APIRequestContext,
                   method: string, url: string,
                   opts: { headers?: Record<string, string>; data?: string } = {}):
  Promise<{ status: number; body: unknown }> {
  const res = await request.fetch(url, {
    method,
    headers: { "Content-Type": "application/json", ...opts.headers },
    data: opts.data,
  });
  let body: unknown = null;
  try { body = await res.json(); } catch { body = await res.text().catch(() => null); }
  return { status: res.status(), body };
}

test.describe("round 2 — tenant boundary", () => {
  test("a workflow def in one tenant is invisible to another", async ({ request }) => {
    // Provision a SECOND tenant + key (admin scope) so we can attempt
    // the cross-tenant read with a valid (but wrong-tenant) credential.
    const otherKey = process.env.E2E_ALT_TENANT_KEY;
    if (!otherKey) {
      test.skip(true, "set E2E_ALT_TENANT_KEY to exercise cross-tenant boundary");
    }
    const OTHER_TENANT = process.env.E2E_ALT_TENANT_ID ?? "e2e_other";
    const OTHER_PID = process.env.E2E_ALT_TENANT_PID ?? "e2e_other_proj";

    // Create a def under e2e (primary tenant).
    const defRes = await raw(request, "POST",
      `${BASE}/tenants/e2e/projects/e2e_proj/workflow-definitions`,
      { headers: { Authorization: `Bearer ${E2E_KEY}` },
        data: JSON.stringify({ name: `xb_${Date.now()}`, steps: [{ id: "s", type: "action", prompt: "x" }] }) });
    expect(defRes.status).toBe(201);
    const defId = (defRes.body as { def_id: string }).def_id;

    // Try to read it as the other tenant — must 404, not 200 (no info leak).
    const cross = await raw(request, "GET",
      `${BASE}/tenants/${OTHER_TENANT}/projects/${OTHER_PID}/workflow-definitions/${defId}`,
      { headers: { Authorization: `Bearer ${otherKey}` } });
    expect(cross.status).toBe(404);

    // Also try listing the OTHER tenant's defs — must not contain our def_id.
    const list = await raw(request, "GET",
      `${BASE}/tenants/${OTHER_TENANT}/projects/${OTHER_PID}/workflow-definitions`,
      { headers: { Authorization: `Bearer ${otherKey}` } });
    expect(list.status).toBe(200);
    const items = (list.body as { def_id: string }[] | null) ?? [];
    expect(items.find((d) => d.def_id === defId)).toBeUndefined();
  });
});

test.describe("round 2 — rate limiter contract", () => {
  test("a single authenticated GET is allowed (smoke)", async ({ request }) => {
    // Full burst behavior is covered by tests/test_round2_b6.py —
    // e2e just asserts the limiter isn't mis-configured in a way that
    // rejects the very first request.
    const res = await raw(request, "GET",
      `${BASE}/tenants/e2e/projects/e2e_proj/workflow-definitions`,
      { headers: { Authorization: `Bearer ${E2E_KEY}` } });
    expect([200, 404]).toContain(res.status); // 404 if project unset, fine
  });
});
