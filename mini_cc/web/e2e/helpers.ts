import { test, expect, type Page } from "@playwright/test";

export const TENANT = "e2e";
export const BASE = process.env.E2E_API_BASE ?? "http://127.0.0.1:8002";

/**
 * Sign in via the login form using the e2e API key and the given Page.
 * Returns the same Page for chaining. Bypasses the fixture system to
 * avoid a Babel-transform quirk with Playwright's destructured fixtures.
 */
export async function signIn(page: Page): Promise<Page> {
  const apiKey = process.env.E2E_API_KEY!;
  await page.goto("/");
  await page.evaluate(() => localStorage.clear());
  await page.reload();
  await page.getByLabel("Base URL").fill(BASE);
  await page.getByLabel("Tenant ID").fill(TENANT);
  await page.getByLabel("API key").fill(apiKey);
  await page.getByLabel("Label (optional)").fill("e2e");
  await page.getByRole("button", { name: /sign in/i }).click();
  await expect(page).toHaveURL(/#\/projects$/);
  return page;
}

export { test, expect };
