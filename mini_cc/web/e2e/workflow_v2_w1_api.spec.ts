/**
 * W1 — definition CRUD HTTP contract.
 *
 * Smoke-tests the four REST endpoints exposed in
 * mini_cc/server/routes/workflow_v2.py against the e2e tenant/project:
 *
 *   GET    /workflow-definitions          list
 *   POST   /workflow-definitions          create
 *   GET    /workflow-definitions/{id}     get
 *   PUT    /workflow-definitions/{id}     update (bumps version)
 *   DELETE /workflow-definitions/{id}     delete
 *
 * The UI flow is covered by workflow_v2_def_crud.spec.ts — this is the
 * contract-level backstop.
 */
import { test, expect } from "@playwright/test";
import { BASE, TENANT } from "./helpers";

const PID = "e2e_proj";
const E2E_KEY = process.env.E2E_API_KEY!;
type Json = Record<string, unknown>;

async function call(request: import("@playwright/test").APIRequestContext,
                    method: string, path: string,
                    body?: unknown): Promise<{ status: number; json: Json }> {
  const res = await request.fetch(`${BASE}/tenants/${TENANT}/projects/${PID}${path}`, {
    method,
    headers: { Authorization: `Bearer ${E2E_KEY}`, "Content-Type": "application/json" },
    data: body ? JSON.stringify(body) : undefined,
  });
  let json: Json = {};
  try { json = await res.json() as Json; } catch { /* ok */ }
  return { status: res.status(), json };
}

test.describe("workflow v2 — def CRUD API (W1)", () => {
  test("create → get → list → update (version bump) → delete", async ({ request }) => {
    // Create.
    const name = `w1api_${Date.now()}`;
    const created = await call(request, "POST", "/workflow-definitions", {
      name,
      description: "v1",
      steps: [{ id: "s1", type: "action", prompt: "x" }],
    });
    expect(created.status).toBe(201);
    expect(created.json.def_id).toMatch(/^wfdef_/);
    expect(created.json.version).toBe(1);
    const defId = created.json.def_id as string;

    // Get.
    const got = await call(request, "GET", `/workflow-definitions/${defId}`);
    expect(got.status).toBe(200);
    expect(got.json.name).toBe(name);

    // List — def_id appears.
    const listed = await call(request, "GET", "/workflow-definitions");
    expect(listed.status).toBe(200);
    const ids = (listed.json as unknown as Array<{ def_id: string }>).map((d) => d.def_id);
    expect(ids).toContain(defId);

    // Update — bumps version + rewrites name.
    const updated = await call(request, "PUT", `/workflow-definitions/${defId}`, {
      name: `${name}_v2`,
      description: "v2",
      steps: [
        { id: "s1", type: "action", prompt: "x" },
        { id: "s2", type: "validate", config: { check: "True" } },
      ],
    });
    expect(updated.status).toBe(200);
    expect(updated.json.version).toBe(2);
    expect(updated.json.name).toBe(`${name}_v2`);
    expect((updated.json.steps as Array<Json>).length).toBe(2);

    // Delete.
    const deleted = await call(request, "DELETE", `/workflow-definitions/${defId}`);
    expect(deleted.status).toBe(204);

    // Subsequent GET → 404.
    const gone = await call(request, "GET", `/workflow-definitions/${defId}`);
    expect(gone.status).toBe(404);
  });

  test("list with no defs returns empty array", async ({ request }) => {
    // Use a fresh project for a deterministic empty list.
    const pid = `w1_empty_${Date.now()}`;
    const proj = await call(request, "POST", "", {}); // sanity — uses PID, not this
    void proj;
    // Create a fresh project via the projects endpoint.
    const newProj = await request.fetch(`${BASE}/tenants/${TENANT}/projects`, {
      method: "POST",
      headers: { Authorization: `Bearer ${E2E_KEY}`, "Content-Type": "application/json" },
      data: JSON.stringify({ project_id: pid, display_name: pid }),
    });
    expect(newProj.ok()).toBeTruthy();

    const listed = await request.fetch(`${BASE}/tenants/${TENANT}/projects/${pid}/workflow-definitions`, {
      method: "GET",
      headers: { Authorization: `Bearer ${E2E_KEY}` },
    });
    expect(listed.status()).toBe(200);
    expect(await listed.json()).toEqual([]);
  });

  test("update on a missing def returns 404", async ({ request }) => {
    const res = await call(request, "PUT", "/workflow-definitions/wfdef_doesnotexist", {
      name: "x", steps: [{ id: "s", type: "action", prompt: "" }],
    });
    expect(res.status).toBe(404);
  });
});
