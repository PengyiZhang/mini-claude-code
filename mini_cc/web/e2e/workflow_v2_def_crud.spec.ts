/**
 * W6 — definition CRUD + version-bump edit spec.
 *
 * Exercises the DefinitionEditor modal end-to-end through the UI:
 *   - edit an existing def → save bumps version (PUT /workflow-definitions/{id})
 *   - edited def shows "v2" in the left pane
 *   - delete a def removes it from the left pane
 *
 * No API calls — pure UI mechanics.
 */
import { test, expect } from "@playwright/test";
import { signIn } from "./helpers";

const PID = "e2e_proj";

test.afterEach(async ({ page }) => {
  if (!page.isClosed()) {
    await page.goto("/#/projects").catch(() => {});
  }
});

test.describe("workflow v2 — def CRUD", () => {
  test("edit a definition and bump its version", async ({ page }) => {
    await signIn(page);
    await page.goto(`/#/projects/${PID}/workflows`);

    // Author a fresh def we own.
    await page.getByTitle("new definition").click();
    const name = `edit_${Date.now()}`;
    await page.getByPlaceholder(/release-deploy/i).fill(name);
    await page.getByPlaceholder(/what does this workflow do/i).fill("v1");
    await page.locator("textarea").first().fill("step one");
    await page.getByRole("button", { name: /^create$/ }).click();

    // v1 in left pane.
    const v1Row = page.locator(`text=/${name}.*v1 · 1 step/i`).first();
    await expect(v1Row).toBeVisible({ timeout: 10_000 });

    // Hover the def row to surface the ✎ edit button (it's hidden until
    // hover; see LeftPane.tsx group-hover:opacity-100).
    const defRow = page.locator("button", { hasText: name }).first();
    await defRow.hover();
    await page.getByTitle("edit").first().click();

    // Editor opens in edit mode — header shows the version.
    await expect(page.locator("text=/edit · .*\(v1\)/i")).toBeVisible();

    // Bump description + save. The footer button text changes to
    // "save (bumps version)" in edit mode.
    await page.getByPlaceholder(/what does this workflow do/i).fill("v2");
    await page.getByRole("button", { name: /save \(bumps version\)/i }).click();

    // After save, left pane re-renders with v2.
    await expect(page.locator(`text=/${name}.*v2 · 1 step/i`).first()).toBeVisible({ timeout: 10_000 });
  });

  test("delete a definition removes it from the left pane", async ({ page }) => {
    await signIn(page);
    await page.goto(`/#/projects/${PID}/workflows`);

    // Author a fresh def.
    await page.getByTitle("new definition").click();
    const name = `del_${Date.now()}`;
    await page.getByPlaceholder(/release-deploy/i).fill(name);
    await page.getByPlaceholder(/what does this workflow do/i).fill("to delete");
    await page.locator("textarea").first().fill("x");
    await page.getByRole("button", { name: /^create$/ }).click();
    await expect(page.locator(`text=/${name}.*v1/i`).first()).toBeVisible({ timeout: 10_000 });

    // Open the editor for this def (hover to surface ✎).
    const defRow = page.locator("button", { hasText: name }).first();
    await defRow.hover();
    await page.getByTitle("edit").first().click();

    // Pre-accept the window.confirm prompt the editor shows on delete.
    page.on("dialog", (d) => d.accept());

    // Click the delete button (edit-mode only, far-left of footer).
    await page.getByRole("button", { name: /^delete$/i }).click();

    // Modal closes + left pane no longer contains the def.
    await expect(page.locator(`text=/${name}/i`)).toHaveCount(0, { timeout: 10_000 });
  });
});
