import { defineConfig, devices } from "@playwright/test";

const FRONT = "http://localhost:5173/";
const API = "http://127.0.0.1:8002";

export default defineConfig({
  testDir: "./e2e",
  timeout: 60_000,
  expect: { timeout: 10_000 },
  fullyParallel: false,
  retries: 0,
  workers: 1,
  reporter: [["list"], ["html", { open: "never" }]],
  globalSetup: "./e2e/globalSetup.ts",
  use: {
    baseURL: FRONT,
    trace: "retain-on-failure",
    video: "off",
    screenshot: "only-on-failure",
    actionTimeout: 15_000,
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
  webServer: {
    command: "npm run dev",
    url: FRONT,
    reuseExistingServer: true,
    timeout: 60_000,
    env: {
      VITE_API_BASE: API,
    },
  },
});
