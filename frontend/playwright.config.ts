import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  timeout: 600_000,
  expect: { timeout: 15_000 },
  workers: 1,
  fullyParallel: false,
  reporter: [["list"], ["json", { outputFile: "playwright-report/results.json" }]],
  use: { baseURL: "http://localhost:3000", trace: "retain-on-failure", screenshot: "only-on-failure" },
  projects: [
    { name: "setup", testMatch: /auth\.setup\.ts/ },
    { name: "desktop", use: { ...devices["Desktop Chrome"], storageState: "playwright/.auth/operator.json" }, testMatch: /app\.spec\.ts/, dependencies: ["setup"] },
    { name: "mobile", use: { ...devices["Pixel 7"], storageState: "playwright/.auth/operator.json" }, testMatch: /mobile\.spec\.ts/, dependencies: ["desktop"] },
  ],
  webServer: [
    { command: "bash e2e/backend.sh", url: "http://localhost:8000/health", timeout: 120_000, reuseExistingServer: false },
    { command: "bash e2e/frontend.sh", url: "http://localhost:3000/login", timeout: 120_000, reuseExistingServer: false },
  ],
});
