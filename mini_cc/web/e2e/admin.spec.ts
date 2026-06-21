/**
 * Phase F admin UI: login → keys CRUD → metrics dashboard.
 *
 * Uses E2E_ADMIN_KEY (a `*`-scoped key) provisioned by globalSetup.
 */
import { test, expect } from "@playwright/test";
import { BASE } from "./helpers";

const TENANT = "e2e";

async function adminSignIn(page: import("@playwright/test").Page): Promise<void> {
  const adminKey = process.env.E2E_ADMIN_KEY!;
  await page.goto("/#/admin/login");
  await page.evaluate(() => localStorage.clear());
  await page.reload();
  await page.getByLabel("Base URL").fill(BASE);
  await page.getByLabel("Tenant ID").fill(TENANT);
  await page.getByLabel("Admin key").fill(adminKey);
  await page.getByRole("button", { name: /enter admin/i }).click();
  await expect(page).toHaveURL(/#\/admin\/keys$/);
}

test.describe("admin", () => {
  test("rejects non-admin key", async ({ page }) => {
    // The plain e2e key has default scope (no admin:read).
    const plainKey = process.env.E2E_API_KEY!;
    await page.goto("/#/admin/login");
    await page.evaluate(() => localStorage.clear());
    await page.reload();
    await page.getByLabel("Base URL").fill(BASE);
    await page.getByLabel("Tenant ID").fill(TENANT);
    await page.getByLabel("Admin key").fill(plainKey);
    await page.getByRole("button", { name: /enter admin/i }).click();
    await expect(page.locator("text=/forbidden|admin:read/i")).toBeVisible({
      timeout: 10_000,
    });
  });

  test("admin login → keys listed", async ({ page }) => {
    await adminSignIn(page);
    await expect(page.getByRole("heading", { name: /API keys/ })).toBeVisible({
      timeout: 10_000,
    });
    await expect(page.locator("table tbody tr").first()).toBeVisible({
      timeout: 10_000,
    });
  });

  test("creates a new read-only key", async ({ page }) => {
    await adminSignIn(page);
    await page.getByRole("button", { name: /new key/i }).click();
    await page.getByLabel(/scopes/i).fill("read:*");
    await page.getByLabel(/expires in/i).fill("7d");
    await page.getByLabel(/label/i).fill(`pw_${Date.now()}`);
    await page.getByRole("button", { name: /^new key$/i }).click();
    // Modal should close.
    await expect(page.getByText(/rotated from/i)).toBeVisible({ timeout: 10_000 });
  });

  test("metrics dashboard renders", async ({ page }) => {
    await adminSignIn(page);
    await page.getByRole("link", { name: /metrics/i }).click();
    await expect(page).toHaveURL(/#\/admin\/metrics$/);
    // Drive some traffic from inside the browser context so the
    // tenant has at least one http_requests_total observation.
    await page.evaluate(async (base) => {
      const key = localStorage.getItem("mini_cc.admin.v1");
      const apiKey = key ? (JSON.parse(key) as { apiKey: string }).apiKey : "";
      await fetch(`${base}/tenants/e2e/admin/keys`, {
        headers: { Authorization: `Bearer ${apiKey}` },
      });
    }, BASE);
    // Wait for the auto-refresh tick + card title.
    await expect(page.locator("text=/HTTP requests\\/min|In-flight/i").first()).toBeVisible({
      timeout: 10_000,
    });
  });
});
