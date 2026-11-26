// Shared set-up for the smoke tests: a small CSV dataset created through the
// REST API, and removal of everything a test made.
import { expect, request, type APIResponse, type Browser, type Page } from "@playwright/test";
import { SET_PASSWORD_LINK, fakeMailURL } from "../playwright.config";

export const CSV = [
  "id,fund,channel,amount,gift_date",
  "1,Library,Online,100,2026-01-15",
  "2,Library,Mail,200,2026-02-15",
  "3,Sport,Online,300,2026-03-15",
  "4,Sport,Event,400,2026-04-15",
].join("\n");

export interface Fixture {
  stamp: string;
  dataset: string;
  datasetId: string;
  connectionId: string;
}

/** Problems the page reported: script errors and Content Security Policy violations. */
export function watchPage(page: Page): string[] {
  const problems: string[] = [];
  page.on("pageerror", (error) => problems.push(`page error: ${error.message}`));
  page.on("console", (message) => {
    if (message.type() === "error" && /content security policy/i.test(message.text())) {
      problems.push(`csp: ${message.text()}`);
    }
  });
  return problems;
}

async function csrf(page: Page): Promise<Record<string, string>> {
  const cookies = await page.context().cookies();
  const token = cookies.find((cookie) => cookie.name === "opsis_csrf");
  if (!token) throw new Error("No opsis_csrf cookie. Open a page of the app first.");
  return { "X-CSRF-Token": decodeURIComponent(token.value) };
}

async function body(response: APIResponse, what: string): Promise<any> {
  if (!response.ok()) throw new Error(`${what} failed: ${response.status()} ${await response.text()}`);
  return response.status() === 204 ? null : response.json();
}

export async function api(page: Page, method: "GET" | "POST" | "PUT" | "PATCH" | "DELETE", path: string, data?: unknown): Promise<any> {
  const headers = method === "GET" ? {} : await csrf(page);
  const response = await page.request.fetch(path, { method, headers, data });
  return body(response, `${method} ${path}`);
}

/** Upload a CSV file. The answer's `file` goes in a file connection's settings. */
export async function uploadCsv(page: Page, name: string, csv: string): Promise<{ file: string }> {
  return body(
    await page.request.post("api/uploads", {
      headers: await csrf(page),
      multipart: { file: { name, mimeType: "text/csv", buffer: Buffer.from(csv) } },
    }),
    "upload",
  );
}

/** Queue a refresh of a dataset and wait until it has succeeded. */
export async function refreshDataset(page: Page, datasetId: string): Promise<void> {
  const job = await api(page, "POST", `api/datasets/${datasetId}/refresh`, {});
  await expect
    .poll(async () => (await api(page, "GET", `api/jobs/${job.id}`)).status, { timeout: 60_000 })
    .toBe("succeeded");
}

/** Upload a CSV (the one above unless given), create a connection and a dataset, and wait for the first refresh. */
export async function createDataset(page: Page, csv: string = CSV): Promise<Fixture> {
  const stamp = Date.now().toString(36);
  const uploaded = await uploadCsv(page, `smoke_${stamp}.csv`, csv);
  const connection = await api(page, "POST", "api/connections", {
    name: `Smoke test ${stamp}`,
    connectorId: "file",
    config: { file: uploaded.file },
  });
  const streams = await api(page, "GET", `api/connections/${connection.id}/streams`);
  const dataset = await api(page, "POST", "api/datasets", {
    name: `smoke_${stamp}`,
    connectionId: connection.id,
    stream: streams[0].name,
  });
  await api(page, "POST", `api/datasets/${dataset.id}/refresh`, {});
  await expect
    .poll(async () => (await api(page, "GET", `api/datasets/${dataset.id}`)).lastJob?.status, { timeout: 60_000 })
    .toBe("succeeded");
  return { stamp, dataset: dataset.name, datasetId: dataset.id, connectionId: connection.id };
}

export async function removeFixture(page: Page, fixture: Fixture | undefined, dashboardIds: string[]): Promise<void> {
  for (const id of dashboardIds) await api(page, "DELETE", `api/dashboards/${id}`).catch(() => undefined);
  if (!fixture) return;
  await api(page, "DELETE", `api/datasets/${fixture.datasetId}`).catch(() => undefined);
  await api(page, "DELETE", `api/connections/${fixture.connectionId}`).catch(() => undefined);
}

export const tiles = (page: Page) => page.locator("[data-item]");

/** Add a visual from the toolbar, choose its dataset, and bind the first offered field to each named role. */
export async function addVisual(page: Page, visual: string, dataset: string, roles: string[]): Promise<void> {
  await page.getByRole("button", { name: visual, exact: true }).click();
  await page.getByLabel("Dataset", { exact: true }).selectOption(dataset);
  for (const role of roles) {
    await page.getByLabel(`Add a field to ${role}`, { exact: true }).selectOption({ index: 1 });
  }
}

export function dashboardIdFrom(url: string): string {
  const match = /dashboards\/([0-9a-f-]{36})/.exec(url);
  if (!match) throw new Error(`No dashboard id in ${url}`);
  return match[1]!;
}

/** What the fake model says when it has built a dashboard, naming whose key answered. */
export const BUILT = /Added a KPI card and a bar chart \((no key|key ending .{4})\)\./;

/** Ask the open editor's assistant to build a dashboard from a dataset; returns the reply.
 * A new provider starts a new conversation, so the newest reply is the last one shown. */
export async function askToBuild(page: Page, dataset: string): Promise<string> {
  const log = page.getByRole("log", { name: "Conversation with the assistant" });
  await page.getByLabel("Message to the assistant").fill(`Build a dashboard from ${dataset}`);
  await page.getByRole("button", { name: "Send", exact: true }).click();
  await expect(page.getByRole("button", { name: "Stop" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Send", exact: true })).toBeVisible({ timeout: 60_000 });
  await expect(log.getByText(BUILT).last()).toBeVisible();
  return (await log.getByText(BUILT).last().textContent()) ?? "";
}

const NOBODY = { cookies: [], origins: [] };
export const PERSON_PASSWORD = "Smoke-Test-Pass-2026";

interface Email {
  to: string;
  subject: string;
  text: string;
}

/** Server mode: invite someone (the invitation is emailed to the fake mail server),
 * then set their password from the link in a browser of their own, signed in. */
export async function invitePerson(
  page: Page,
  browser: Browser,
  name: string,
  role: "editor" | "viewer" = "editor",
): Promise<{ page: Page; id: string; email: string }> {
  const email = `${name.toLowerCase()}-${Date.now().toString(36)}@smoke.test`;
  const made = await api(page, "POST", "api/users", { email, displayName: `${name} Smoke`, role });
  let link: string | null = made.link;
  if (!link) {
    const mailbox = await request.newContext({ storageState: NOBODY });
    try {
      await expect
        .poll(
          async () => {
            const messages = (await (await mailbox.get(`${fakeMailURL}/messages?to=${encodeURIComponent(email)}`)).json()) as Email[];
            link = SET_PASSWORD_LINK.exec(messages.at(-1)?.text ?? "")?.[0] ?? null;
            return link !== null;
          },
          { timeout: 30_000 },
        )
        .toBe(true);
    } finally {
      await mailbox.dispose();
    }
  }
  const context = await browser.newContext({ storageState: NOBODY, viewport: { width: 1500, height: 950 } });
  const person = await context.newPage();
  await person.goto(link!);
  await person.getByLabel("New password").fill(PERSON_PASSWORD);
  await person.getByLabel("Repeat the password").fill(PERSON_PASSWORD);
  await person.getByRole("button", { name: "Set password and sign in" }).click();
  await expect(person.getByRole("navigation", { name: "Main" })).toBeVisible();
  return { page: person, id: made.user.id, email };
}
