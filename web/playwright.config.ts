import { defineConfig } from "@playwright/test";
export default defineConfig({
  testDir: "./e2e",
  use: {
    channel: process.env.PLAYWRIGHT_CHANNEL || "chromium",
    baseURL: "http://127.0.0.1:8766",
    viewport: { width: 1440, height: 1000 },
  },
  webServer: {
    command: `${process.platform === "win32" ? "..\\.venv\\Scripts\\python.exe" : "../.venv/bin/python"} e2e/server.py`,
    url: "http://127.0.0.1:8766/api/books",
    reuseExistingServer: false,
  },
});
