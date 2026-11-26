// Server mode: sign-in, invitations and password resets by email (read from the
// fake mail server, e2e/fake-smtp.mjs), viewers who cannot edit, sharing, emailed
// dashboards, share links, Sign in with Google (against the fake provider), the
// test email, and access tokens for scripts.
// Runs only against the disposable server-mode stack (OPSIS_MODE=server), which is
// removed afterwards, so it does not tidy up after itself.
import { expect, request, test, type Browser, type Page } from "@playwright/test";
import { SET_PASSWORD_LINK, baseURL, fakeMailURL, serverMode } from "../playwright.config";
import { api, createDataset } from "./helpers";

test.skip(!serverMode, "Needs the server-mode stack: npm run smoke runs it.");

const NOBODY = { cookies: [], origins: [] };
const PASSWORD = "Smoke-Test-Pass-2026";

async function freshPage(browser: Browser): Promise<Page> {
  const context = await browser.newContext({ storageState: NOBODY, viewport: { width: 1500, height: 950 } });
  return context.newPage();
}

interface Email {
  to: string;
  subject: string;
  text: string;
  html: string;
}

/** The newest email to an address with this subject, from the fake mail server. */
async function emailTo(address: string, subject: string): Promise<Email> {
  const mailbox = await request.newContext({ storageState: NOBODY });
  let found: Email | undefined;
  try {
    await expect
      .poll(
        async () => {
          const response = await mailbox.get(`${fakeMailURL}/messages?to=${encodeURIComponent(address)}`);
          const messages = (await response.json()) as Email[];
          found = messages.filter((m) => m.subject === subject).at(-1);
          return found !== undefined;
        },
        { timeout: 30_000 },
      )
      .toBe(true);
  } finally {
    await mailbox.dispose();
  }
  return found!;
}

function linkIn(email: Email): string {
  const link = SET_PASSWORD_LINK.exec(email.text)?.[0];
  if (!link) throw new Error(`No set-password link in: ${email.text}`);
  expect(email.html).toContain(`href="${link}"`);
  return link;
}

async function csrfHeader(page: Page): Promise<Record<string, string>> {
  const token = (await page.context().cookies()).find((c) => c.name === "opsis_csrf");
  return { "X-CSRF-Token": decodeURIComponent(token!.value) };
}

test("people sign in; viewers cannot edit; share links stop when revoked; Google; tokens", async ({ page, browser }) => {
  const stamp = Date.now().toString(36);
  await page.goto("dashboards");
  const fixture = await createDataset(page);
  const dataset = fixture.dataset;
  const spec = (title: string) => ({
    specVersion: 1,
    title,
    layout: { columns: 12, rowHeight: 40 },
    filters: [],
    items: [
      { id: "k", visual: "kpi", position: { x: 0, y: 0, w: 3, h: 3 }, dataset, bindings: { value: [`${dataset}.total_amount`] } },
      {
        id: "b", visual: "bar", position: { x: 3, y: 0, w: 6, h: 7 }, dataset,
        bindings: { category: [`${dataset}.fund`], value: [`${dataset}.total_amount`] },
      },
    ],
    interactions: { crossFilter: true },
  });
  const title = `Shared ${stamp}`;
  const dashboard = await api(page, "POST", "api/dashboards", { title, spec: spec(title) });
  await api(page, "POST", `api/dashboards/${dashboard.id}/publish`);

  await test.step("an admin invites a viewer by email; she sets a password from the email", async () => {
    await page.goto("users");
    await page.getByLabel("Email", { exact: true }).fill(`vera-${stamp}@smoke.test`);
    await page.getByLabel("Name", { exact: true }).fill("Vera Smoke");
    await page.getByLabel("Role", { exact: true }).selectOption("viewer");
    await page.getByRole("button", { name: "Invite", exact: true }).click();
    await expect(page.getByRole("status").filter({ hasText: `Invitation emailed to vera-${stamp}@smoke.test` })).toBeVisible();
    await expect(page.getByLabel("One-time link")).toHaveCount(0);
    const invitation = await emailTo(`vera-${stamp}@smoke.test`, "You are invited to Opsis");
    expect(invitation.text).toContain("Hello Vera Smoke,");
    const link = linkIn(invitation);

    const vera = await freshPage(browser);
    await vera.goto(link);
    await expect(vera.getByRole("heading", { name: "Welcome" })).toBeVisible();
    await vera.getByLabel("New password").fill(PASSWORD);
    await vera.getByLabel("Repeat the password").fill(PASSWORD);
    await vera.getByRole("button", { name: "Set password and sign in" }).click();
    await expect(vera.getByRole("navigation", { name: "Main" })).toBeVisible();
    await vera.goto("dashboards");
    await expect(vera.getByText(title)).toHaveCount(0); // not shared yet

    await test.step("the dashboard is shared with her; she can view, not edit", async () => {
      await page.goto(`dashboards/${dashboard.id}`);
      await page.getByRole("button", { name: "Share" }).click();
      await page.getByLabel("Share with").selectOption({ label: `Vera Smoke (vera-${stamp}@smoke.test)` });
      await page.getByRole("button", { name: "Add", exact: true }).click();
      await page.getByRole("button", { name: "Save sharing" }).click();
      await expect(page.getByText("Not saved yet.")).toHaveCount(0);

      await vera.goto("dashboards");
      await vera.getByRole("link", { name: title }).click();
      await expect(vera.getByRole("heading", { name: title })).toBeVisible();
      await expect(vera.locator("[data-item]").first()).toContainText(/\d/);
      await expect(vera.getByRole("link", { name: "Edit" })).toHaveCount(0);
      await expect(vera.getByRole("button", { name: "Share" })).toHaveCount(0);
      await vera.goto(`dashboards/${dashboard.id}/edit`);
      await expect(vera.getByText("You do not have permission to edit dashboards.")).toBeVisible();
      const put = await vera.request.fetch(`api/dashboards/${dashboard.id}`, {
        method: "PUT",
        headers: await csrfHeader(vera),
        data: { spec: spec("Vera was here") },
      });
      expect(put.status()).toBe(403);
    });

    await test.step("she has the dashboard emailed to her, and stops it from her account", async () => {
      await vera.goto(`dashboards/${dashboard.id}`);
      await vera.getByRole("button", { name: "Email me" }).click();
      await expect(vera.getByText(`Sent to vera-${stamp}@smoke.test`, { exact: false })).toBeVisible();
      await vera.getByLabel("When").selectOption({ label: "Mondays at 07:00" });
      await vera.getByRole("button", { name: "Save", exact: true }).click();
      await expect(vera.getByText(/^Next email /)).toBeVisible();
      await vera.goto("account");
      const row = vera.getByRole("row").filter({ hasText: title });
      await expect(row).toContainText("Mondays at 07:00");
      await row.getByRole("button", { name: `Stop emailing ${title}` }).click();
      await expect(vera.getByText("None yet.")).toBeVisible();
    });

    await test.step("Forgotten your password? emails a link that sets a new one", async () => {
      const someone = await freshPage(browser);
      await someone.goto("");
      await someone.getByRole("button", { name: "Forgotten your password?" }).click();
      await someone.getByLabel("Email me a link to set a new password").fill(`vera-${stamp}@smoke.test`);
      await someone.getByRole("button", { name: "Send the link" }).click();
      await expect(someone.getByRole("status")).toContainText("If there is an account for that address");
      const reset = await emailTo(`vera-${stamp}@smoke.test`, "Set a new password for Opsis");
      await someone.goto(linkIn(reset));
      await expect(someone.getByRole("heading", { name: "Choose a new password" })).toBeVisible();
      await someone.getByLabel("New password").fill(`${PASSWORD}-2`);
      await someone.getByLabel("Repeat the password").fill(`${PASSWORD}-2`);
      await someone.getByRole("button", { name: "Save and sign in" }).click();
      await expect(someone.getByRole("navigation", { name: "Main" })).toBeVisible();
      expect((await api(someone, "GET", "api/me")).email).toBe(`vera-${stamp}@smoke.test`);
    });
  });

  await test.step("an admin sends a test email from Settings", async () => {
    await page.goto("settings");
    await page.getByRole("button", { name: "Send me a test email" }).click();
    await expect(page.getByRole("status").filter({ hasText: "A test email was sent to" })).toBeVisible();
    const me = (await api(page, "GET", "api/me")).email as string;
    expect((await emailTo(me, "Test email from Opsis")).text).toContain("Email works.");
  });

  await test.step("a share link shows the published version, and stops when revoked", async () => {
    await page.goto(`dashboards/${dashboard.id}`);
    await page.getByRole("button", { name: "Share" }).click();
    await page.getByLabel("Name", { exact: true }).fill("Board");
    await page.getByRole("button", { name: "Create link" }).click();
    const url = (await page.getByLabel("One-time link").innerText()).trim();
    expect(url).toMatch(/\/opsis\/s\/[A-Za-z0-9_-]{43}$/);
    // A draft change is saved but not published: the link must not show it.
    await api(page, "PUT", `api/dashboards/${dashboard.id}`, { spec: spec("Unpublished draft") });

    const stranger = await freshPage(browser);
    await stranger.goto(url);
    await expect(stranger.getByRole("heading", { name: title })).toBeVisible();
    await expect(stranger.getByText("Unpublished draft")).toHaveCount(0);
    await expect(stranger.locator("[data-item]")).toHaveCount(2);
    await expect(stranger.locator("[data-item] canvas").first()).toBeVisible();

    await page.reload();
    await page.getByRole("button", { name: "Share" }).click();
    await page.getByRole("button", { name: /^Revoke link Board/ }).click();
    await expect(page.getByRole("button", { name: /^Revoke link Board/ })).toHaveCount(0);
    await stranger.reload();
    await expect(stranger.getByRole("heading", { name: "This link is not available" })).toBeVisible();
  });

  await test.step("Sign in with Google: an invited, allowed address gets in; others do not", async () => {
    await api(page, "POST", "api/users", { email: `pat-${stamp}@allowed.test`, role: "editor" });
    const pat = await freshPage(browser);
    await pat.goto("");
    await pat.getByRole("link", { name: "Sign in with Google" }).click();
    await expect(pat.getByRole("heading", { name: "Choose an account" })).toBeVisible();
    await pat.getByLabel("Email").fill(`pat-${stamp}@allowed.test`);
    await pat.getByRole("button", { name: "Continue" }).click();
    await expect(pat.getByRole("navigation", { name: "Main" })).toBeVisible();
    expect((await api(pat, "GET", "api/me")).email).toBe(`pat-${stamp}@allowed.test`);

    const eve = await freshPage(browser);
    await eve.goto("");
    await eve.getByRole("link", { name: "Sign in with Google" }).click();
    await eve.getByLabel("Email").fill(`eve-${stamp}@other.test`);
    await eve.getByRole("button", { name: "Continue" }).click();
    await expect(eve.getByRole("alert")).toHaveText("That Google account is not allowed to sign in to this workspace.");
    expect((await eve.request.get("api/me")).status()).toBe(401);
  });

  await test.step("a script uses an access token on the REST API, within its scopes", async () => {
    const made = await api(page, "POST", "api/tokens", { name: "script", scopes: ["read"], days: 1 });
    // storageState: NOBODY, or the runner would hand these contexts the admin session.
    const script = await request.newContext({ baseURL, storageState: NOBODY, extraHTTPHeaders: { Authorization: `Bearer ${made.token}` } });
    expect((await script.get("api/dashboards")).status()).toBe(200);
    expect((await script.post("api/dashboards", { data: { title: "Nope" } })).status()).toBe(403);
    const anonymous = await request.newContext({ baseURL, storageState: NOBODY });
    expect((await anonymous.get("api/dashboards")).status()).toBe(401);
    await script.dispose();
    await anonymous.dispose();
  });
});
