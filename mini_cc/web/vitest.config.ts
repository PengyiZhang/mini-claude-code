import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test-setup.ts"],
    // Playwright owns the e2e/ directory; vitest shouldn't try to load
    // test.describe() blocks from there.
    exclude: ["**/node_modules/**", "**/dist/**", "e2e/**"],
  },
});
