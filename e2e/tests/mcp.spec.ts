// The MCP server, driven by the official MCP client over HTTP, with an editor
// open in the browser: an agent builds a dashboard and the editor shows it live.
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";
import { expect, test } from "@playwright/test";
import { baseURL } from "../playwright.config";
import { api, createDataset, removeFixture, tiles, watchPage, type Fixture } from "./helpers";

interface ToolOutcome {
  ok: boolean;
  text: string;
  data: any;
}

/** An MCP client. With MCP_AUTH=token it sends a personal access token, as Claude Code would. */
async function connect(token?: string): Promise<Client> {
  const client = new Client({ name: "opsis-e2e", version: "1.0.0" });
  const headers: Record<string, string> = token ? { Authorization: `Bearer ${token}` } : {};
  await client.connect(new StreamableHTTPClientTransport(new URL("mcp", baseURL), { requestInit: { headers } }));
  return client;
}

/** A token for the MCP client when the stack wants one; undefined otherwise. */
async function mcpToken(page: import("@playwright/test").Page, scopes: string[]): Promise<string | undefined> {
  const config = await api(page, "GET", "api/config");
  if (config.mcpAuth !== "token") return undefined;
  const made = await api(page, "POST", "api/tokens", { name: "smoke test", scopes, days: 1 });
  return made.token as string;
}

async function call(client: Client, name: string, args: Record<string, unknown> = {}): Promise<ToolOutcome> {
  const result = (await client.callTool({ name, arguments: args })) as { isError?: boolean; content: { type: string; text: string }[] };
  const text = result.content[0]?.text ?? "";
  return { ok: !result.isError, text, data: result.isError ? null : JSON.parse(text) };
}

async function must(client: Client, name: string, args: Record<string, unknown> = {}): Promise<any> {
  const outcome = await call(client, name, args);
  expect(outcome.ok, `${name} failed: ${outcome.text}`).toBe(true);
  return outcome.data;
}

test("an agent builds a dashboard over MCP and an open editor follows it live", async ({ page }) => {
  const problems = watchPage(page);
  const dashboards: string[] = [];
  let fixture: Fixture | undefined;
  let client: Client | undefined;
  await page.goto("dashboards");

  try {
    fixture = await createDataset(page);
    const dataset = fixture.dataset;
    client = await connect(await mcpToken(page, ["read", "edit", "publish"]));

    await test.step("discover", async () => {
      const listed = await client!.listTools();
      const names = listed.tools.map((tool) => tool.name);
      expect(names).toEqual(expect.arrayContaining(["list_datasets", "describe_dataset", "list_visuals", "create_dashboard", "patch_dashboard"]));
      expect((await client!.listResources()).resources.map((r) => r.uri)).toContain("opsis://guide");
      const prompt = await client!.getPrompt({ name: "build_dashboard", arguments: { dataset } });
      expect(JSON.stringify(prompt.messages)).toContain(dataset);

      const datasets = await must(client!, "list_datasets");
      expect(datasets.datasets.map((d: any) => d.dataset)).toContain(dataset);
      const described = await must(client!, "describe_dataset", { dataset });
      expect(described.rowCount).toBe(4);
      expect(described.dimensions.map((d: any) => d.ref)).toContain(`${dataset}.fund`);
      const rows = await must(client!, "run_query", { query: { dataset, measures: [`${dataset}.total_amount`], dimensions: [`${dataset}.fund`], sort: [{ field: `${dataset}.fund`, dir: "asc" }] } });
      expect(rows.rows).toEqual([["Library", 300], ["Sport", 700]]);
    });

    // An empty dashboard, open in the editor before the agent adds anything.
    const created = await must(client, "create_dashboard", { title: `Agent ${fixture.stamp}` });
    dashboards.push(created.id);
    // The links an agent hands to the user point at this install, sub-path included.
    expect(created.editUrl).toBe(new URL(`dashboards/${created.id}/edit`, baseURL).toString());
    const streaming = page.waitForResponse((response) => response.url().includes(`/dashboards/${created.id}/events`));
    await page.goto(`dashboards/${created.id}/edit`);
    expect((await streaming).headers()["content-type"]).toContain("text/event-stream");
    await expect(tiles(page)).toHaveCount(0);
    const status = page.getByRole("status").filter({ hasText: /Saved as version|Unsaved changes/ });
    const notice = page.getByRole("alert").filter({ hasText: "Updated by assistant." });

    await test.step("the agent's change appears without a reload", async () => {
      const visuals = (await must(client!, "list_visuals", { dataset })).visuals;
      const example = (id: string) => structuredClone(visuals.find((v: any) => v.visual === id).exampleItem);
      const kpi = { ...example("kpi"), position: { x: 0, y: 0, w: 3, h: 3 } };
      const bar = { ...example("bar"), position: { x: 3, y: 0, w: 5, h: 7 } };
      const combo = {
        id: "combo_axis",
        visual: "combo",
        title: "Amount and number of gifts",
        position: { x: 0, y: 7, w: 8, h: 8 },
        dataset,
        bindings: { category: [`${dataset}.fund`], bars: [`${dataset}.total_amount`], lines: [{ field: `${dataset}.row_count`, axis: "right" }] },
      };
      const ops = [kpi, bar, combo].map((value) => ({ op: "add", path: "/items/-", value }));

      // A stale base version is refused with instructions, and the agent recovers.
      await must(client!, "patch_dashboard", { id: created.id, base_version: 1, ops: [{ op: "add", path: "/description", value: "Built by an agent" }] });
      const stale = await call(client!, "patch_dashboard", { id: created.id, base_version: 1, ops });
      expect(stale.ok).toBe(false);
      expect(stale.text).toContain("Error (version_conflict)");
      expect(stale.text).toContain("get_dashboard");
      const latest = await must(client!, "get_dashboard", { id: created.id });
      const saved = await must(client!, "patch_dashboard", { id: created.id, base_version: latest.version, ops, message: "Add three visuals" });
      expect(saved.itemCount).toBe(3);

      await expect(tiles(page)).toHaveCount(3);
      await expect(notice).toContainText("Add three visuals");
      await expect(status).toHaveText(`Saved as version ${saved.version}`);
      await expect(tiles(page).nth(2).locator("canvas")).toBeVisible();
      await expect(tiles(page).nth(0)).toContainText(/\d/);
      await tiles(page).nth(2).scrollIntoViewIfNeeded();
      await page.waitForTimeout(600); // let the chart finish its entry animation for the picture
      await tiles(page).nth(2).screenshot({ path: test.info().outputPath("combo-right-axis.png") });
    });

    await test.step("Undo takes the editor back to the version before the agent's change", async () => {
      await notice.getByRole("button", { name: "Undo" }).click();
      await expect(tiles(page)).toHaveCount(0);
      await expect(notice).toHaveCount(0);
      const versions = await must(client!, "list_versions", { id: created.id });
      expect(versions.versions[0].message).toBe("Restored version 2");
      expect(versions.versions[0].authorType).toBe("user");
      expect(versions.versions[1].authorType).toBe("agent");
    });

    await test.step("unsaved work in the editor is not overwritten", async () => {
      await page.getByRole("button", { name: "Text", exact: true }).click();
      await expect(status).toHaveText("Unsaved changes");
      const latest = await must(client!, "get_dashboard", { id: created.id });
      const again = await must(client!, "restore_version", { id: created.id, version: latest.version - 1 });
      const waiting = page.getByRole("alert").filter({ hasText: "while you have unsaved changes" });
      await expect(waiting).toBeVisible();
      await expect(tiles(page)).toHaveCount(1); // still the local, unsaved text tile
      await waiting.getByRole("button", { name: `Load version ${again.version}` }).click();
      await expect(tiles(page)).toHaveCount(3);
      await expect(status).toHaveText(`Saved as version ${again.version}`);
    });

    await test.step("a wrong field name is explained well enough to fix", async () => {
      const latest = await must(client!, "get_dashboard", { id: created.id });
      const wrong = await call(client!, "patch_dashboard", {
        id: created.id,
        base_version: latest.version,
        ops: [{ op: "replace", path: "/items/0/bindings/value/0", value: `${dataset}.total_amont` }],
      });
      expect(wrong.ok).toBe(false);
      expect(wrong.text).toContain("/items/0/bindings/value/0");
      expect(wrong.text).toContain(`Did you mean '${dataset}.total_amount'`);
      expect((await must(client!, "get_dashboard", { id: created.id })).version).toBe(latest.version);
    });

    expect(problems).toEqual([]);
  } finally {
    await client?.close().catch(() => undefined);
    await removeFixture(page, fixture, dashboards);
  }
});

test("an access token is shown once and can be revoked", async ({ page }) => {
  const problems = watchPage(page);
  await page.goto("tokens");
  const name = `Smoke ${Date.now().toString(36)}`;
  await expect(page.getByLabel("Command to add Opsis to Claude Code")).toContainText(new URL("mcp", baseURL).toString());
  await page.getByLabel("Name").fill(name);
  await page.getByRole("button", { name: "Create token" }).click();
  const secret = (await page.getByLabel("New access token").innerText()).trim();
  expect(secret).toMatch(/^opsis_pat_[A-Za-z0-9_-]{40,}$/);
  await page.getByRole("button", { name: "Done" }).click();
  await page.reload();
  const row = page.getByRole("row").filter({ hasText: name });
  await expect(row).toContainText(`${secret.slice(0, 16)}...`);
  await expect(page.getByText(secret)).toHaveCount(0); // never shown again
  await row.getByRole("button", { name: `Revoke ${name}` }).click();
  await expect(row).toContainText("Revoked");
  await row.getByRole("button", { name: `Delete ${name}` }).click();
  await expect(row).toHaveCount(0);
  expect(problems).toEqual([]);
});
