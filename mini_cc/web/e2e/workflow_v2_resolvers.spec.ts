/**
 * W2 reject + W3 webhook_wait + W5 validate — resolver lifecycle specs.
 *
 * Same hybrid pattern as workflow_v2_advanced.spec.ts: API for setup/drive,
 * UI to verify visible state and exercise resolver buttons.
 *
 * All three step types pause or terminate deterministically without an
 * LLM call (validate evaluates an expression; webhook_wait parks until a
 * POST hits the inbound endpoint; checkpoint reject just fails the run).
 */
import { test, expect } from "@playwright/test";
import { signIn, BASE, TENANT } from "./helpers";

const PID = "e2e_proj";
type Json = Record<string, unknown>;

async function api(request: import("@playwright/test").APIRequestContext,
                   method: "GET" | "POST" | "PUT" | "DELETE",
                   path: string,
                   body?: unknown): Promise<Json> {
  const key = process.env.E2E_API_KEY!;
  const res = await request.fetch(`${BASE}/tenants/${TENANT}/projects/${PID}${path}`, {
    method,
    headers: {
      "Authorization": `Bearer ${key}`,
      "Content-Type": "application/json",
    },
    data: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok()) {
    throw new Error(`${method} ${path} -> ${res.status()}: ${await res.text()}`);
  }
  return res.json() as Promise<Json>;
}

/** Drive a run via API and return the resulting run state. */
async function drive(request: import("@playwright/test").APIRequestContext,
                     runId: string, sid: string): Promise<Json> {
  return api(request, "POST", `/workflow-runs/${runId}/drive`, { session_id: sid });
}

async function newDefAndRun(request: import("@playwright/test").APIRequestContext,
                            steps: Array<Record<string, unknown>>,
                            namePrefix: string): Promise<{ defId: string; runId: string; sid: string }> {
  const def = await api(request, "POST", "/workflow-definitions", {
    name: `${namePrefix}_${Date.now()}`,
    description: "e2e",
    steps,
  });
  const sess = await api(request, "POST", "/sessions", {});
  const run = await api(request, "POST", `/workflow-definitions/${def.def_id}/runs`, {});
  return { defId: def.def_id as string, runId: run.run_id as string, sid: sess.session_id as string };
}

test.afterEach(async ({ page }) => {
  if (!page.isClosed()) {
    await page.goto("/#/projects").catch(() => {});
  }
});

test.describe("workflow v2 — resolvers", () => {
  test("W2: checkpoint Reject fails the run (API)", async ({ request }) => {
    // Reject goes through the same UI wiring as Approve (covered in
    // workflow_v2_advanced.spec.ts) — exercise the API contract directly
    // to assert the run terminates with status=failed.
    const { runId, sid } = await newDefAndRun(request, [
      { id: "gate", type: "checkpoint", config: {} },
    ], "reject");

    const driven = await drive(request, runId, sid);
    expect(driven.status).toBe("paused");

    const after = await api(request, "POST",
      `/workflow-runs/${runId}/steps/gate/resolve`,
      { decision: "reject", approver: "e2e", feedback: "no" });
    expect(after.status).toBe("failed");
    const stepRun = (after.step_runs as Array<Record<string, unknown>>)[0];
    expect(stepRun.status).toBe("failed");
  });

  test("W3: webhook_wait resolves via inbound webhook POST", async ({ request }) => {
    const WHID = `wh_${Date.now()}`;
    const { runId, sid } = await newDefAndRun(request, [
      { id: "wait", type: "webhook_wait", config: { webhook_id: WHID } },
      { id: "after", type: "action", prompt: "ok" },
    ], "whwait");

    const driven = await drive(request, runId, sid);
    expect(driven.status).toBe("paused");
    expect((driven as Json).current_step_idx).toBe(0);

    // External system POSTs the inbound webhook. The endpoint accepts
    // either a tenant bearer OR the webhook_id query param as a shared
    // secret — exercise the shared-secret path (no Authorization header).
    const res = await request.fetch(
      `${BASE}/tenants/${TENANT}/projects/${PID}/workflow-runs/${runId}/webhook/wait?webhook_id=${WHID}`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        data: JSON.stringify({ event: "build.success", ts: Date.now() }),
      },
    );
    expect(res.ok()).toBeTruthy();
    const after = (await res.json()) as Json;
    // Per resolve_webhook_wait: the wait step is marked completed and
    // current_step_idx advances; if there are remaining steps the run
    // STAYS paused (waiting for the next drive). Assert step-level
    // state, not run-level.
    const stepRuns = after.step_runs as Array<Record<string, unknown>>;
    const waitStep = stepRuns.find((s) => s.step_id === "wait")!;
    expect(waitStep.status).toBe("completed");
    expect(after.current_step_idx).toBe(1);
  });

  test("W5: validate step fails the run on falsy check", async ({ request }) => {
    const { runId, sid } = await newDefAndRun(request, [
      { id: "v", type: "validate", config: { check: "False" } },
    ], "vfail");

    const driven = await drive(request, runId, sid);
    expect(driven.status).toBe("failed");
    // The validate step records its error on the step run.
    const stepRun = (driven as Json).step_runs as Array<Record<string, unknown>>;
    expect(stepRun[0].status).toBe("failed");
  });

  test("W5: validate step passes on truthy check", async ({ request }) => {
    const { runId, sid } = await newDefAndRun(request, [
      { id: "v", type: "validate", config: { check: "1 + 1 == 2" } },
    ], "vpass");

    const driven = await drive(request, runId, sid);
    // Truthy validate passes — run completes (no further steps).
    expect(driven.status).toBe("completed");
    const stepRun = (driven as Json).step_runs as Array<Record<string, unknown>>;
    expect(stepRun[0].status).toBe("completed");
  });
});
