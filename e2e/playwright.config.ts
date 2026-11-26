// Smoke tests against a test stack. `npm run smoke` at the repository root starts
// each one (scripts/test-stack.mjs) and sets OPSIS_URL:
//   http://localhost:18080/         the root
//   http://localhost:18088/opsis/   behind the proxytest nginx
//   http://localhost:18089/opsis/   server mode, with sign-in (OPSIS_MODE=server)
// They never run against your own stack: port 8080 is refused.
import { defineConfig } from "@playwright/test";
import { STATE } from "./global-setup";

const raw = process.env.OPSIS_URL ?? "http://localhost:18080/";
if (new URL(raw).port === "8080") {
  throw new Error(
    `Refusing to run the smoke tests against ${raw}: port 8080 is your own stack's. Run npm run smoke, which starts a test stack of its own.`,
  );
}
// A trailing slash matters: the tests navigate with relative paths.
export const baseURL = raw.endsWith("/") ? raw : `${raw}/`;
/** The fake Google (e2e/fake-google.mjs) and the fake mail server's messages, as the
 * browser reaches them on the test stack. */
export const fakeGoogleURL = process.env.OPSIS_FAKE_GOOGLE_URL ?? "http://localhost:19700";
export const fakeMailURL = process.env.OPSIS_FAKE_MAIL_URL ?? "http://localhost:19600";
/** A set-password link in an email: the stack's own address, then the token. */
export const SET_PASSWORD_LINK = new RegExp(`${baseURL.replace(/[.*+?^${}()|[\]\\/]/g, "\\$&")}set-password/[A-Za-z0-9_-]+`);
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
