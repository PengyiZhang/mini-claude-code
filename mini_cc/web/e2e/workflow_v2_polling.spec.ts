/**
 * W6 — run polling auto-refresh spec.
 *
 * Verifies the polling loop in workflowV2Store.startRunPolling: when a
 * paused run is selected in the UI and an external trigger advances it
 * (here, an inbound webhook POST), the UI status updates without a
 * manual refresh.
 *
 * Pattern:
 *   - UI: select a paused webhook_wait run
 *   - API: POST inbound webhook → run advances to "completed" (last step)
 *   - UI: within ~5s the left-pane status badge swaps from ⏸ paused
 *     to ✓ completed
 */
import { test, expect } from "@playwright/test";
import { signIn, BASE, TENANT } from "./helpers";

const PID = "e2e_proj";
const E2E_KEY = process.env.E2E_API_KEY!;
type Json = Record<string, unknown>;

async function api(request: import("@playwright/test").APIRequestContext,
                   method: string, path: string, body?: unknown): Promise<Json> {
  const res = await request.fetch(`${BASE}/tenants/${TENANT}/projects/${PID}${path}`, {
    method,
    headers: { Authorization: `Bearer ${E2E_KEY}`, "Content-Type": "application/json" },
    data: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok()) throw new Error(`${method} ${path} -> ${res.status()}: ${await res.text()}`);
  return res.json() as Promise<Json>;
}

test.afterEach(async ({ page }) => {
  if (!page.isClosed()) await page.goto("/#/projects").catch(() => {});
});

test.describe("workflow v2 — run polling", () => {
  test("UI picks up external webhook resolution without manual refresh", async ({ page, request }) => {
    const WHID = `wh_${Date.now()}`;
    const defName = `poll_${Date.now()}`;

    // Single webhook_wait step — resolving it completes the run, so
    // polling observes paused → completed.
    const def = await api(request, "POST", "/workflow-definitions", {
      name: defName, description: "polling e2e",
      steps: [{ id: "wait", type: "webhook_wait", config: { webhook_id: WHID } }],
    });
    const defId = def.def_id as string;
    const sess = await api(request, "POST", "/sessions", {});
    const run = await api(request, "POST", `/workflow-definitions/${defId}/runs`, {});
    const runId = run.run_id as string;
    const driven = await api(request, "POST", `/workflow-runs/${runId}/drive`,
      { session_id: sess.session_id });
    expect(driven.status).toBe("paused");

    // ── UI: open the workflow page and select the run.
    await signIn(page);
    await page.goto(`/#/projects/${PID}/workflows`);

    // Select our def, then our run.
    await page.getByText(defName).first().click();
    const runBtn = page.locator(`text=#${runId.replace(/^wfrun_/, "")}`).first();
    await runBtn.waitFor({ state: "visible", timeout: 10_000 });
    await runBtn.click();

    // Sanity: left pane status badge shows paused.
    await expect(page.locator(`text=/⏸\\s*paused/i`).first()).toBeVisible({ timeout: 10_000 });

    // ── External trigger: POST inbound webhook (no UI involvement).
    const res = await request.fetch(
      `${BASE}/tenants/${TENANT}/projects/${PID}/workflow-runs/${runId}/webhook/wait?webhook_id=${WHID}`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        data: JSON.stringify({ event: "build.success" }),
      },
    );
    expect(res.ok()).toBeTruthy();

    // ── UI: polling (2s interval) should pick up the new state within
    // ~3 ticks. Generous 12s timeout to absorb jitter.
    await expect(page.locator(`text=/✓\\s*completed/i`).first()).toBeVisible({ timeout: 12_000 });
  });
});
