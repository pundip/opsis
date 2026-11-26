// The in-app assistant, answered by the scripted fake model (e2e/fake-llm.mjs):
// asked to build a dashboard it works step by step, the canvas follows along,
// each step opens to show what it did, a change can be undone with one click,
// and the conversation is still there after a reload. At the root and the
// sub-path the workspace's option is Ollama; in server mode it is the workspace's
// Claude key, which an admin must first allow (v2 V1). Then people's own Anthropic
// and OpenAI keys, from the Account page to a build with each.
import { expect, test } from "@playwright/test";
import { serverMode } from "../playwright.config";
import { BUILT, type Fixture, api, askToBuild, createDataset, removeFixture, tiles, watchPage } from "./helpers";

test.skip(!process.env.OPSIS_FAKE_MODEL, "Needs the fake model: run npm run smoke.");

test("the assistant builds a dashboard, and its change can be undone", async ({ page }) => {
  const problems = watchPage(page);
  await page.goto("dashboards");
  const config = await api(page, "GET", "api/config");
  expect(config.features.assistant).toBe(true);
  let fixture: Fixture | undefined;
  const dashboards: string[] = [];
  const me = await api(page, "GET", "api/me");
  // With sign-in nobody may use the workspace key until an admin allows them.
  if (serverMode) await api(page, "PUT", "api/assistant-access/grants", { grantee: `user:${me.id}`, allowed: true });
  try {
    fixture = await createDataset(page);
    const dashboard = await api(page, "POST", "api/dashboards", { title: `Assistant ${fixture.stamp}` });
    dashboards.push(dashboard.id);
    await page.goto(`dashboards/${dashboard.id}/edit`);
    await page.getByRole("tab", { name: "Assistant" }).click();
    const provider = config.assistantProvider === "anthropic" ? "Claude" : "Ollama";
    await expect(page.getByText(`${provider} (${config.assistantModel})`, { exact: true })).toBeVisible();

    await page.getByLabel("Message to the assistant").fill(`Build a dashboard from ${fixture.dataset}`);
    await page.getByRole("button", { name: "Send", exact: true }).click();
    // While it works there is a Stop button; then its reply, and three steps.
    await expect(page.getByRole("button", { name: "Stop" })).toBeVisible();
    const log = page.getByRole("log", { name: "Conversation with the assistant" });
    await expect(log.getByText(BUILT)).toBeVisible({ timeout: 60_000 });
    // Ollama has no key; the workspace's Claude key is the one smoke.mjs made.
    const whose = serverMode ? `key ending ${process.env.OPSIS_WORKSPACE_KEY_END ?? ""}` : "no key";
    await expect(log.getByText(BUILT)).toContainText(whose);
    await expect(page.getByRole("button", { name: "Send", exact: true })).toBeVisible();
    const steps = log.locator("[data-step]");
    await expect(steps).toHaveCount(3);
    await expect(log.locator('[data-step="patch_dashboard"]')).toContainText("Saved version 2");
    if (provider === "Claude") await expect(log.getByText(`Looking at ${fixture.dataset}.`)).toBeVisible();

    // The canvas followed along, without a banner: the panel shows the change.
    await expect(page.getByRole("heading", { name: "Built by the assistant" })).toBeVisible();
    await expect(tiles(page)).toHaveCount(2);
    await expect(page.getByText("Updated by assistant.")).toHaveCount(0);
    await expect(page.getByText("Saved as version 2")).toBeVisible();

    // A step opens to show what it sent and what came back.
    const describe = log.locator('[data-step="describe_dataset"]');
    await describe.locator("summary").click();
    await expect(describe).toContainText(`"dataset": "${fixture.dataset}"`);

    // One click undoes the change, as a new version.
    const patch = log.locator('[data-step="patch_dashboard"]');
    await patch.getByRole("button", { name: "Undo" }).click();
    await expect(tiles(page)).toHaveCount(0);
    await expect(page.getByText("Saved as version 3")).toBeVisible();
    await expect(patch).toContainText("Restored version 1");
    const versions = await api(page, "GET", `api/dashboards/${dashboard.id}/versions`);
    expect(versions.map((v: { authorType: string }) => v.authorType)).toEqual(["user", "agent", "user"]);

    // The conversation is still there after a reload.
    await page.reload();
    await page.getByRole("tab", { name: "Assistant" }).click();
    await expect(log.getByText(`Build a dashboard from ${fixture.dataset}`, { exact: true })).toBeVisible();
    await expect(steps).toHaveCount(3);
    await expect(log.getByText(/3 steps, [\d,]+ tokens in/)).toBeVisible();
    expect(problems).toEqual([]);
  } finally {
    if (serverMode) await api(page, "PUT", "api/assistant-access/grants", { grantee: `user:${me.id}`, allowed: null });
    await removeFixture(page, fixture, dashboards);
  }
});

test("people's own Anthropic and OpenAI keys build dashboards, and are never shown again", async ({ page }) => {
  const problems = watchPage(page);
  const stamp = Date.now().toString(36);
  const claudeKey = `sk-ant-api03-smoke-${stamp}-${"A".repeat(24)}C1au`;
  const openaiKey = `sk-proj-smoke-${stamp}-${"B".repeat(24)}Op3n`;
  // Names unique to this run: a deleted key's use stays on the Account page under its name.
  const claude = `Smoke Claude ${stamp}`;
  const openai = `Smoke OpenAI ${stamp}`;
  const wrong = `Smoke wrong ${stamp}`;
  let fixture: Fixture | undefined;
  const dashboards: string[] = [];
  const addKey = async (provider: string, name: string, key: string, model = "") => {
    await page.getByLabel("Provider", { exact: true }).selectOption(provider);
    await page.getByLabel("Name", { exact: true }).fill(name);
    await page.getByLabel("API key", { exact: true }).fill(key);
    await page.getByLabel("Default model", { exact: true }).fill(model);
    await page.getByRole("button", { name: "Add key", exact: true }).click();
  };
  try {
    // The Account page: add a key, and it is tested at once (free: the provider lists
    // its models). Only its name and last four characters are ever shown.
    await page.goto("account");
    await expect(page.getByRole("heading", { name: "AI providers" })).toBeVisible();
    await addKey("anthropic", claude, claudeKey);
    await expect(page.getByRole("status").filter({ hasText: `${claude}: The key works: it may use 2 models.` })).toBeVisible();
    await addKey("openai", openai, openaiKey, "gpt-5.5");
    await expect(page.getByRole("status").filter({ hasText: `${openai}: The key works: it may use 2 models.` })).toBeVisible();
    await addKey("anthropic", wrong, `sk-ant-api03-bad-${stamp}-${"C".repeat(24)}`);
    await expect(page.getByRole("status").filter({ hasText: `${wrong}: Anthropic did not accept the key: invalid x-api-key` })).toBeVisible();
    const keys = page.getByRole("table", { name: "Your API keys" });
    await expect(keys.getByRole("row", { name: new RegExp(wrong) })).toContainText("Refused");
    await page.getByRole("button", { name: `Delete ${wrong}` }).click();
    await expect(keys.getByRole("row", { name: new RegExp(wrong) })).toHaveCount(0);
    await expect(keys.getByRole("row", { name: new RegExp(claude) })).toContainText(`ending ${claudeKey.slice(-4)}`);
    // A default model, chosen from the key's own list.
    await page.getByRole("button", { name: `Edit ${claude}` }).click();
    await keys.getByLabel("Default model").fill("claude-opus-5-5");
    await keys.getByRole("button", { name: "Save", exact: true }).click();
    await expect(keys.getByRole("row", { name: new RegExp(claude) })).toContainText("claude-opus-5-5");
    const listing = await page.request.get("api/account/api-keys");
    const shown = await listing.text();
    expect(shown).not.toContain(claudeKey);
    expect(shown).not.toContain(openaiKey);
    expect(await page.content()).not.toContain(claudeKey);

    // The editor: each key answers its own requests.
    await page.goto("dashboards");
    fixture = await createDataset(page);
    const dashboard = await api(page, "POST", "api/dashboards", { title: `Own keys ${fixture.stamp}` });
    dashboards.push(dashboard.id);
    await page.goto(`dashboards/${dashboard.id}/edit`);
    await page.getByRole("tab", { name: "Assistant" }).click();
    const picker = page.getByLabel("AI provider");
    await picker.selectOption({ label: `${claude} (your Anthropic key ending ${claudeKey.slice(-4)})` });
    await expect(page.getByLabel("Model", { exact: true })).toHaveValue("claude-opus-5-5");
    expect(await askToBuild(page, fixture.dataset)).toContain(`key ending ${claudeKey.slice(-4)}`);
    await picker.selectOption({ label: `${openai} (your OpenAI key ending ${openaiKey.slice(-4)})` });
    await expect(page.getByLabel("Model", { exact: true })).toHaveValue("gpt-5.5");
    await expect(page.getByText("OpenAI (gpt-5.5)", { exact: true })).toBeVisible();
    expect(await askToBuild(page, fixture.dataset)).toContain(`key ending ${openaiKey.slice(-4)}`);
    await expect(tiles(page)).toHaveCount(2);

    // Back on the Account page: what each key was used for; then delete them.
    await page.goto("account");
    const usage = page.getByRole("table", { name: "Your assistant use" });
    await expect(usage.getByRole("row", { name: new RegExp(claude) })).toContainText("Claude");
    await expect(usage.getByRole("row", { name: new RegExp(openai) })).toContainText("OpenAI");
    await page.getByRole("button", { name: `Delete ${claude}` }).click();
    await page.getByRole("button", { name: `Delete ${openai}` }).click();
    await expect(page.getByRole("table", { name: "Your API keys" })).toHaveCount(0);
    expect(problems).toEqual([]);
  } finally {
    const left = await api(page, "GET", "api/account/api-keys").catch(() => ({ keys: [] }));
    for (const key of left.keys as { id: string; label: string }[]) {
      if (key.label.startsWith("Smoke")) await api(page, "DELETE", `api/account/api-keys/${key.id}`).catch(() => undefined);
    }
    await removeFixture(page, fixture, dashboards);
  }
});
