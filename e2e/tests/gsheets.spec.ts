// Google Sheets through your own Google account (OAuth), against a fake Google
// (e2e/fake-google.mjs on port 9700). At the root, local mode: Google comes straight
// back to Opsis. Under /opsis and in server mode: you paste the address Google sent
// the browser to. Then a spreadsheet is picked and refreshed; Google stops accepting
// the sign-in (invalid_grant), the connection asks to be reconnected, and is.
import { expect, request, test, type BrowserContext, type Locator, type Page } from "@playwright/test";
import { baseURL, serverMode } from "../playwright.config";
import { api, watchPage } from "./helpers";

const FAKE = "http://localhost:9700";
const CREDENTIALS = JSON.stringify({
  installed: {
    client_id: "smoke-client.apps.googleusercontent.com",
    project_id: "opsis-smoke",
    auth_uri: `${FAKE}/o/oauth2/auth`, // the browser goes here
    token_uri: "http://fake-google:9700/token", // the api container goes here
    client_secret: "smoke-secret",
    redirect_uris: ["http://localhost"],
  },
});
// At the root Opsis is on a loopback address with no sub-path, so Google can come straight back.
const direct = !new URL(baseURL).pathname.startsWith("/opsis");

/** Allow access on the fake Google, and finish however this install finishes. */
async function connectGoogle(page: Page, context: BrowserContext, panel: Locator, button: string): Promise<void> {
  if (direct) {
    await panel.getByRole("button", { name: button }).click();
    await page.waitForURL(/localhost:9700\/o\/oauth2\/auth/);
    await page.getByRole("button", { name: "Allow" }).click();
    await page.waitForURL(/connections\?google=/);
    return;
  }
  // Google sends the new tab to http://localhost/?state=...&code=..., which does not load.
  await context.route(
    (url) => url.hostname === "localhost" && url.port === "" && url.searchParams.has("state"),
    (route) => route.fulfill({ status: 200, contentType: "text/html", body: "<p>Copy this address.</p>" }),
  );
  const opened = context.waitForEvent("page");
  await panel.getByRole("button", { name: button }).click();
  const google = await opened;
  await google.getByRole("button", { name: "Allow" }).click();
  await google.waitForURL((url) => url.searchParams.has("code"));
  const address = google.url();
  await google.close();
  await panel.getByLabel("The address from the address bar").fill(address);
  await panel.getByRole("button", { name: "Finish connecting" }).click();
}

test("a Google Sheets connection signs in with my own Google account", async ({ page, context }) => {
  const problems = watchPage(page);
  const stamp = Date.now().toString(36);
  const name = `Sheets ${stamp}`;
  let connectionId: string | undefined;
  let datasetId: string | undefined;
  try {
    await test.step("a new connection with the OAuth client (credentials.json)", async () => {
      await page.goto("connections");
      await page.getByRole("button", { name: "New connection" }).click();
      await page.getByLabel("Type").selectOption({ label: "Google Sheets" });
      // Required fields carry a * in their label.
      await page.getByLabel(/^Name/).fill(name);
      await expect(page.getByLabel(/^Sign in with/)).toHaveValue("oauth");
      await expect(page.getByLabel("Service account key")).toHaveCount(0); // only for the other method
      await page.getByLabel("OAuth client (credentials.json)").fill(CREDENTIALS);
      await page.getByRole("button", { name: "Save connection" }).click();
      const row = page.getByRole("row").filter({ hasText: name });
      await expect(row).toContainText("Not connected");
      connectionId = (await api(page, "GET", "api/connections")).find((c: { name: string }) => c.name === name).id;
    });

    const panel = page.locator("section").filter({ has: page.getByRole("heading", { name: `Google account for ${name}` }) });
    await test.step(direct ? "connect: Google comes straight back" : "connect: paste the address", async () => {
      await page.getByRole("button", { name: `Google account for ${name}` }).click();
      await expect(panel).toContainText("refresh tokens after 7 days");
      await connectGoogle(page, context, panel, "Connect Google account");
      // The status line, and after a pasted address also "Connected as ...".
      await expect(panel.getByText("smoke@gmail.test").first()).toBeVisible();
      await expect(page.getByRole("row").filter({ hasText: name })).toContainText("Connected");
    });

    await test.step("pick a spreadsheet and read it", async () => {
      await panel.getByRole("button", { name: "Use Smoke giving" }).click();
      await expect(panel.getByText("In use")).toBeVisible();
      const dataset = await api(page, "POST", "api/datasets", { name: `sheet_${stamp}`, connectionId, stream: "Gifts" });
      datasetId = dataset.id;
      await api(page, "POST", `api/datasets/${datasetId}/refresh`, {});
      await expect
        .poll(async () => (await api(page, "GET", `api/datasets/${datasetId}`)).lastJob?.status, { timeout: 60_000 })
        .toBe("succeeded");
      expect((await api(page, "GET", `api/datasets/${datasetId}/preview`)).rowCount).toBe(3);
    });

    await test.step("Google stops accepting the sign-in, and it is reconnected", async () => {
      const fake = await request.newContext();
      await fake.post(`${FAKE}/revoke-all`);
      await fake.dispose();
      await api(page, "POST", `api/datasets/${datasetId}/refresh`, {});
      await expect
        .poll(async () => (await api(page, "GET", `api/datasets/${datasetId}`)).lastJob?.status, { timeout: 60_000 })
        .toBe("failed");
      await page.goto(`connections?google=${connectionId}`);
      await expect(page.getByRole("row").filter({ hasText: name })).toContainText("Needs reconnecting");
      await expect(panel.getByRole("alert")).toContainText("invalid_grant");
      if (serverMode) {
        // The owner is emailed (the stand-in mail server of the server-mode stack).
        const mailbox = await request.newContext();
        await expect
          .poll(async () => {
            const messages = (await (await mailbox.get("http://localhost:9600/messages")).json()) as { subject: string }[];
            return messages.some((m) => m.subject === `Reconnect ${name} in Opsis`);
          })
          .toBe(true);
        await mailbox.dispose();
      }
      await connectGoogle(page, context, panel, "Reconnect");
      await expect(page.getByRole("row").filter({ hasText: name })).toContainText("Connected");
      await api(page, "POST", `api/datasets/${datasetId}/refresh`, {});
      await expect
        .poll(async () => (await api(page, "GET", `api/datasets/${datasetId}`)).lastJob?.status, { timeout: 60_000 })
        .toBe("succeeded");
    });
  } finally {
    if (datasetId) await api(page, "DELETE", `api/datasets/${datasetId}`).catch(() => undefined);
    if (connectionId) await api(page, "DELETE", `api/connections/${connectionId}`).catch(() => undefined);
  }
  expect(problems).toEqual([]);
});
