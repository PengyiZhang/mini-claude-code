/**
 * W4 — email_wait inbound resolver spec.
 *
 * The inbound email resolver is HTTP-triggered (an external pipeline
 * like SendGrid Inbound Parse POSTs to /email/{step_id}). No real
 * IMAP/SMTP server is needed — the resolver just matches filters and
 * advances the run, so we exercise it directly.
 *
 * Covers three scenarios:
 *   - matching email advances the step (paused → completed)
 *   - non-matching email is rejected with 400 (BadRequest)
 *   - resolver on a non-paused step is rejected with 400
 */
import { test, expect } from "@playwright/test";
import { BASE, TENANT } from "./helpers";

const PID = "e2e_proj";
const E2E_KEY = process.env.E2E_API_KEY!;
type Json = Record<string, unknown>;

async function api(request: import("@playwright/test").APIRequestContext,
                   method: "GET" | "POST" | "PUT" | "DELETE",
                   path: string,
                   body?: unknown): Promise<{ status: number; json: Json }> {
  const res = await request.fetch(`${BASE}/tenants/${TENANT}/projects/${PID}${path}`, {
    method,
    headers: {
      "Authorization": `Bearer ${E2E_KEY}`,
      "Content-Type": "application/json",
    },
    data: body ? JSON.stringify(body) : undefined,
  });
  let json: Json = {};
  try { json = await res.json() as Json; } catch { /* keep empty */ }
  return { status: res.status(), json };
}

async function newDefRunSession(request: import("@playwright/test").APIRequestContext,
                                 steps: Array<Record<string, unknown>>,
                                 namePrefix: string): Promise<{ defId: string; runId: string; sid: string }> {
  const def = await api(request, "POST", "/workflow-definitions", {
    name: `${namePrefix}_${Date.now()}`,
    description: "e2e",
    steps,
  });
  const sess = await api(request, "POST", "/sessions", {});
  const run = await api(request, "POST", `/workflow-definitions/${def.json.def_id}/runs`, {});
  return {
    defId: def.json.def_id as string,
    runId: run.json.run_id as string,
    sid: sess.json.session_id as string,
  };
}

test.describe("workflow v2 — email_wait resolver (W4)", () => {
  test("matching email advances the step", async ({ request }) => {
    const { runId, sid } = await newDefRunSession(request, [
      { id: "wait", type: "email_wait",
        config: { from_filter: "@company.com", subject_filter: "[APPROVE]" } },
      { id: "after", type: "action", prompt: "ok" },
    ], "emailok");

    const driven = await api(request, "POST",
      `/workflow-runs/${runId}/drive`, { session_id: sid });
    expect(driven.json.status).toBe("paused");

    // Matching inbound email.
    const res = await api(request, "POST",
      `/workflow-runs/${runId}/email/wait`,
      { from: "alice@company.com", to: "bot@mini.cc",
        subject: "[APPROVE] deploy", body: "go ahead" });
    expect(res.status).toBe(200);
    const waitStep = (res.json.step_runs as Array<Json>)[0];
    expect(waitStep.status).toBe("completed");
    expect(res.json.current_step_idx).toBe(1);
  });

  test("non-matching email is rejected (400) and step stays paused", async ({ request }) => {
    const { runId, sid } = await newDefRunSession(request, [
      { id: "wait", type: "email_wait",
        config: { from_filter: "@company.com" } },
    ], "emailbad");

    const driven = await api(request, "POST",
      `/workflow-runs/${runId}/drive`, { session_id: sid });
    expect(driven.json.status).toBe("paused");

    // Wrong sender domain → must be rejected.
    const res = await api(request, "POST",
      `/workflow-runs/${runId}/email/wait`,
      { from: "stranger@evil.com", subject: "ignore filters", body: "" });
    expect(res.status).toBe(400);

    // Verify the run is still paused at the wait step.
    const after = await api(request, "GET", `/workflow-runs/${runId}`);
    expect(after.json.status).toBe("paused");
    const waitStep = (after.json.step_runs as Array<Json>)[0];
    expect(waitStep.status).toBe("paused");
  });

  test("resolving a non-paused step is rejected (400)", async ({ request }) => {
    const { runId, sid } = await newDefRunSession(request, [
      { id: "wait", type: "email_wait",
        config: { from_filter: "@company.com" } },
    ], "emailstate");

    // Drive — pauses at the wait step. Resolve it successfully.
    await api(request, "POST", `/workflow-runs/${runId}/drive`, { session_id: sid });
    const first = await api(request, "POST",
      `/workflow-runs/${runId}/email/wait`,
      { from: "ok@company.com", subject: "", body: "" });
    expect(first.status).toBe(200);

    // Second resolution attempt — step is now completed, not paused.
    const second = await api(request, "POST",
      `/workflow-runs/${runId}/email/wait`,
      { from: "ok@company.com", subject: "", body: "" });
    expect(second.status).toBe(400);
  });
});
