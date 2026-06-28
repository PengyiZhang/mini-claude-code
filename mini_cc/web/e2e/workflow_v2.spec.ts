/**
 * W6 e2e — Workflow V2 visual editor smoke test.
 *
 * Exercises the three-pane layout end-to-end:
 *   - navigate to /projects/:pid/workflows
 *   - open the new-definition editor and author a 2-step workflow
 *     (action + checkpoint)
 *   - confirm the definition appears in the left pane
 *   - start a run and confirm the timeline renders
 *
 * Doesn't drive the run through a real AgentLoop (that needs a live
 * LiteLLM backend with a valid key) — the focus is UI mechanics.
 */
import { test, expect } from "@playwright/test";
import { signIn, BASE } from "./helpers";

test.afterEach(async ({ page }) => {
  if (!page.isClosed()) {
    await page.goto("/#/projects").catch(() => {});
  }
});

test.describe("workflow v2", () => {
  test("authors a definition and starts a run", async ({ page }) => {
    await signIn(page);
    await page.goto("/#/projects/e2e_proj/workflows");

    // Page shell rendered.
    await expect(page.locator("text=/definitions/i").first()).toBeVisible({ timeout: 10_000 });

    // Open the new-definition editor. The button has only "＋" as visible text
    // — its accessible name comes from the title attribute, so getByRole
    // name-match doesn't find it. Use getByTitle instead.
    await page.getByTitle("new definition").click();
    await expect(page.locator("text=/new workflow definition/i")).toBeVisible();

    // Author: name + description + one action step + one checkpoint.
    await page.getByPlaceholder(/release-deploy/i).fill(`e2e_${Date.now()}`);
    await page.getByPlaceholder(/what does this workflow do/i).fill("smoke");

    // First step is pre-filled as an action — give it a prompt.
    await page.locator("textarea").first().fill("say hi");

    // Add a checkpoint step.
    await page.getByRole("button", { name: /add step/i }).click();

    // Save.
    await page.getByRole("button", { name: /^create$/ }).click();

    // Definition appears in the left pane after save.
    await expect(page.locator("text=/v1 · 2 steps/i").first()).toBeVisible({ timeout: 10_000 });

    // Def is auto-selected on creation; with no runs yet the middle pane
    // shows a "start new run" affordance (added after the W6 UX gap where
    // the empty state had no start button). Click it to start a run.
    await page.getByRole("button", { name: /start new run/i }).click();

    // The right-pane run timeline should show "run started".
    await expect(page.locator("text=/run started/i")).toBeVisible({ timeout: 10_000 });
  });

  test("renders empty state when no definitions exist", async ({ page }) => {
    // Use a fresh project so we don't have to clean up.
    await signIn(page);
    const pid = `wf_empty_${Date.now()}`;
    await page.goto("/#/projects");
    await page.getByRole("button", { name: /new project/i }).click();
    await page.getByPlaceholder(/project_id/i).fill(pid);
    await page.getByPlaceholder(/display name/i).fill(pid);
    await page.getByRole("button", { name: /^create$/ }).click();
    await page.goto(`#/projects/${pid}/workflows`);

    await expect(page.locator("text=/no workflow definitions/i")).toBeVisible({ timeout: 10_000 });
  });
});
