import { test, expect } from "@playwright/test";
import { signIn } from "./helpers";

test.describe("rich cards", () => {
  test.beforeEach(async ({ page }) => {
    await signIn(page);
    await page.goto("/#/projects/e2e_proj");
    await page.getByRole("button", { name: /chat/i }).click();
    await page.getByRole("button", { name: /new session/i }).click();
  });

  test("/config renders a key_value card with expected rows", async ({ page }) => {
    const textarea = page.locator("textarea").first();
    await textarea.fill("/config");
    await page.getByRole("button", { name: /^send$/i }).click();

    // Card shell renders a header with the title.
    await expect(page.locator("text=/Configuration/i").first()).toBeVisible({ timeout: 15_000 });
    // Key-value rows are present.
    await expect(page.locator("text=Model").first()).toBeVisible({ timeout: 10_000 });
    await expect(page.locator("text=API key").first()).toBeVisible({ timeout: 10_000 });
  });

  test("/agents renders a list card (empty-state or roster)", async ({ page }) => {
    const textarea = page.locator("textarea").first();
    await textarea.fill("/agents");
    await page.getByRole("button", { name: /^send$/i }).click();

    // Either the empty-state hint ("no teammates") or a Teammates card title.
    await expect(
      page.locator("text=/Teammates|no teammates/i").first(),
    ).toBeVisible({ timeout: 15_000 });
  });
});
