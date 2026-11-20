// The in-app assistant, answered by the scripted fake model (e2e/fake-llm.mjs):
// asked to build a dashboard it works step by step, the canvas follows along,
// each step opens to show what it did, a change can be undone with one click,
// and the conversation is still there after a reload. At the root and the
// sub-path this goes through the Ollama provider, in server mode through Claude's.
import { expect, test } from "@playwright/test";
import { type Fixture, api, createDataset, removeFixture, tiles, watchPage } from "./helpers";

test.skip(!process.env.OPSIS_FAKE_MODEL, "Needs the fake model: run npm run smoke.");

test("the assistant builds a dashboard, and its change can be undone", async ({ page }) => {
  const problems = watchPage(page);
  await page.goto("dashboards");
  const config = await api(page, "GET", "api/config");
  expect(config.features.assistant).toBe(true);
  let fixture: Fixture | undefined;
  const dashboards: string[] = [];
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
    await expect(log.getByText("Added a KPI card and a bar chart.")).toBeVisible({ timeout: 60_000 });
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
    await removeFixture(page, fixture, dashboards);
  }
});
