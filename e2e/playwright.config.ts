// Smoke tests against a running stack. OPSIS_URL chooses where:
//   http://localhost:8080/         the root
//   http://localhost:8088/opsis/   behind the proxytest profile
// `npm run smoke` at the repository root runs both in turn.
import { defineConfig } from "@playwright/test";

const raw = process.env.OPSIS_URL ?? "http://localhost:8080/";
// A trailing slash matters: the tests navigate with relative paths.
export const baseURL = raw.endsWith("/") ? raw : `${raw}/`;

export default defineConfig({
  testDir: "./tests",
  timeout: 120_000,
  expect: { timeout: 15_000 },
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [["list"]],
  use: {
    baseURL,
    browserName: "chromium",
    viewport: { width: 1500, height: 950 },
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
});
