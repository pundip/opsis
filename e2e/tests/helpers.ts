// Shared set-up for the smoke tests: a small CSV dataset created through the
// REST API, and removal of everything a test made.
import { expect, type APIResponse, type Page } from "@playwright/test";

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

export async function api(page: Page, method: "GET" | "POST" | "PUT" | "DELETE", path: string, data?: unknown): Promise<any> {
  const headers = method === "GET" ? {} : await csrf(page);
  const response = await page.request.fetch(path, { method, headers, data });
  return body(response, `${method} ${path}`);
}

/** Upload a CSV (the one above unless given), create a connection and a dataset, and wait for the first refresh. */
export async function createDataset(page: Page, csv: string = CSV): Promise<Fixture> {
  const stamp = Date.now().toString(36);
  const uploaded = await body(
    await page.request.post("api/uploads", {
      headers: await csrf(page),
      multipart: { file: { name: `smoke_${stamp}.csv`, mimeType: "text/csv", buffer: Buffer.from(csv) } },
    }),
    "upload",
  );
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
