/**
 * W2 + W6 — checkpoint gate lifecycle (UI + API hybrid).
 *
 * Setup is API-level (create session, def with checkpoint as the first
 * step so we don't need an LLM call to reach the gate). Drive also goes
 * through the API. The UI portion verifies:
 *   - paused gate renders inline Approve/Reject controls
 *   - clicking Approve resolves the gate
 *   - run advances past the checkpoint
 *
 * Run via:
 *   E2E_API_KEY=mck_... npx playwright test e2e/workflow_v2_advanced.spec.ts
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
    const text = await res.text();
    throw new Error(`${method} ${path} -> ${res.status()}: ${text}`);
  }
  return res.json() as Promise<Json>;
}

test.afterEach(async ({ page }) => {
  if (!page.isClosed()) {
    await page.goto("/#/projects").catch(() => {});
  }
});

test.describe("workflow v2 — checkpoint gate", () => {
  test("pauses at checkpoint and Approve advances the run", async ({ page, request }) => {
    // ── Setup via API: def with checkpoint as FIRST step.
    // First-step checkpoint means drive pauses immediately without
    // needing to invoke the AgentLoop (no LLM call required).
    const defName = `cp_${Date.now()}`;
    const def = await api(request, "POST", "/workflow-definitions", {
      name: defName,
      description: "checkpoint gate e2e",
      steps: [
        { id: "gate", type: "checkpoint", config: {} },
        { id: "after", type: "action", prompt: "say ok" },
      ],
    });
    const defId = def.def_id as string;

    // Create session + run.
    const sess = await api(request, "POST", "/sessions", {});
    const sid = sess.session_id as string;
    const run = await api(request, "POST", `/workflow-definitions/${defId}/runs`, {});
    const runId = run.run_id as string;

    // Drive — should pause at the checkpoint.
    const driven = await api(request, "POST", `/workflow-runs/${runId}/drive`,
      { session_id: sid });
    expect(driven.status).toBe("paused");

    // ── UI: navigate to the workflow page and select the run.
    await signIn(page);
    await page.goto(`/#/projects/${PID}/workflows`);

    // Wait for defs to load and click our def in the left pane.
    await page.getByText(defName).first().click();

    // The runs section should list our run — click it.
    const runBtn = page.locator(`text=#${runId.replace(/^wfrun_/, "")}`).first();
    await runBtn.waitFor({ state: "visible", timeout: 10_000 });
    await runBtn.click();

    // Middle pane: paused gate renders with an inline Approve button.
    // The button label is "✓ approve" (per RunView.tsx StepTimelineEntry).
    const approveBtn = page.getByRole("button", { name: /approve/i }).first();
    await expect(approveBtn).toBeVisible({ timeout: 10_000 });

    // ── Approve via UI.
    await approveBtn.click();

    // The run should advance past the gate. After approval the next step
    // is "after" (an action that drives through the AgentLoop). The exact
    // terminal status depends on whether the LLM call succeeds, but the
    // gate step itself must move out of "paused".
    // Easiest stable assertion: the "gate" step's status text in the
    // timeline is no longer "paused".
    await expect(page.locator("text=/gate.*paused/i")).toHaveCount(0, { timeout: 10_000 });
  });
});
