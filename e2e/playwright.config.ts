// Smoke tests against a running stack. OPSIS_URL chooses where:
//   http://localhost:8080/         the root
//   http://localhost:8088/opsis/   behind the proxytest profile
// `npm run smoke` at the repository root runs them at the root and the sub-path,
// then against a server-mode stack with sign-in (OPSIS_MODE=server).
import { defineConfig } from "@playwright/test";
import { STATE } from "./global-setup";

const raw = process.env.OPSIS_URL ?? "http://localhost:8080/";
// A trailing slash matters: the tests navigate with relative paths.
export const baseURL = raw.endsWith("/") ? raw : `${raw}/`;
/** "server" when the stack under test has sign-in, share links and tokens. */
export const serverMode = process.env.OPSIS_MODE === "server";

export default defineConfig({
  testDir: "./tests",
  globalSetup: "./global-setup.ts",
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
    // Signed in as the admin when the stack has sign-in (see global-setup.ts).
    storageState: STATE,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
});
