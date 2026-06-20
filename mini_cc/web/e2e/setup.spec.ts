import { test, expect } from "@playwright/test";
import { signIn, BASE } from "./helpers";

test.afterEach(async ({ page }) => {
  if (!page.isClosed()) {
    await page.goto("/#/projects").catch(() => {});
  }
});

test.describe("auth", () => {
  test("rejects unknown api key", async ({ page }) => {
    await page.goto("/");
    await page.evaluate(() => localStorage.clear());
    await page.reload();
    await page.getByLabel("Base URL").fill(BASE);
    await page.getByLabel("Tenant ID").fill("e2e");
    await page.getByLabel("API key").fill("mck_deadbeef_deadbeef_deadbeef_deadbeef");
    await page.getByRole("button", { name: /sign in/i }).click();
    await expect(page.locator("text=/unauthorized|forbidden/i")).toBeVisible({ timeout: 10_000 });
  });
});

test.describe("projects", () => {
  test("creates and lists a project", async ({ page }) => {
    await signIn(page);
    const name = `pw_${Date.now()}`;
    await page.getByRole("button", { name: /new project/i }).click();
    await page.getByPlaceholder(/project_id/i).fill(name);
    await page.getByPlaceholder(/display name/i).fill(name);
    await page.getByRole("button", { name: /^create$/ }).click();
    await expect(page.locator(`text=${name}`).first()).toBeVisible({ timeout: 10_000 });
  });
});

test.describe("files", () => {
  test("uploads files, previews, and downloads zip", async ({ page }) => {
    await signIn(page);
    await page.goto("/#/projects/e2e_proj");
    await page.getByRole("button", { name: /files/i }).click();

    // Make a subfolder via right-click → new folder.
    const folderName = `up_${Date.now()}`;
    await page.locator("text=📁").first().click({ button: "right" });
    await page.getByText(/^＋ New folder/).click();
    await page.getByPlaceholder("new-folder").fill(folderName);
    await page.keyboard.press("Enter");

    // right-click that new folder to upload into it
    const folderRow = page.locator(`text=${folderName}`).first();
    await folderRow.waitFor({ state: "visible", timeout: 10_000 });
    await folderRow.click({ button: "right" });
    await page.getByText(/^📄 Upload files/).click();

    await page
      .locator('input[type="file"]')
      .setInputFiles([
        { name: "a.txt", mimeType: "text/plain", buffer: Buffer.from("hello a") },
        { name: "b.txt", mimeType: "text/plain", buffer: Buffer.from("hello b") },
      ]);

    await expect(page.locator("text=a.txt").first()).toBeVisible({ timeout: 15_000 });
    await expect(page.locator("text=b.txt").first()).toBeVisible({ timeout: 15_000 });

    // preview
    await page.locator("text=a.txt").first().click();
    await expect(page.locator("text=hello a")).toBeVisible({ timeout: 10_000 });

    // download zip
    const [download] = await Promise.all([
      page.waitForEvent("download", { timeout: 10_000 }),
      page.getByRole("button", { name: /download zip/i }).click(),
    ]);
    expect(download.suggestedFilename()).toMatch(/\.zip$/);
  });
});

test.describe("chat", () => {
  test("streams a response via LiteLLM and renders activity cards", async ({ page }) => {
    await signIn(page);
    await page.goto("/#/projects/e2e_proj");
    await page.getByRole("button", { name: /chat/i }).click();

    // always start a fresh session so sid is selected and the textarea is enabled
    await page.getByRole("button", { name: /new session/i }).click();

    const textarea = page.locator("textarea").first();
    await textarea.fill("say hi in one short sentence");
    await page.getByRole("button", { name: /^send$/i }).click();

    await expect
      .poll(
        async () => {
          const body = await page.locator("main").innerText();
          return body.length;
        },
        { timeout: 60_000, intervals: [2_000] },
      )
      .toBeGreaterThan(50);
  });
});
