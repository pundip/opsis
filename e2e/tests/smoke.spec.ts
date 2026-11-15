// The dashboard smoke test: build, save, reload, restore, publish.
// It makes its own small dataset and removes everything afterwards.
import { expect, test } from "@playwright/test";
import { addVisual, createDataset, dashboardIdFrom, removeFixture, tiles, watchPage, type Fixture } from "./helpers";

test("build a dashboard, save, reload, restore and publish", async ({ page }) => {
  const problems = watchPage(page);
  const dashboards: string[] = [];
  let fixture: Fixture | undefined;
  await page.goto("dashboards");

  try {
    fixture = await createDataset(page);
    const status = page.getByRole("status").filter({ hasText: /Saved as version|Unsaved changes/ });

    await test.step("build", async () => {
      await page.getByLabel("Name of the new dashboard").fill(`Smoke ${fixture!.stamp}`);
      await page.getByRole("button", { name: "New dashboard" }).click();
      await expect(page).toHaveURL(/dashboards\/[0-9a-f-]{36}\/edit$/);
      dashboards.push(dashboardIdFrom(page.url()));

      await addVisual(page, "KPI card", fixture!.dataset, ["Value"]);
      await addVisual(page, "Bar chart", fixture!.dataset, ["Category", "Values"]);
      await expect(tiles(page)).toHaveCount(2);
      // The KPI shows a number computed from the four rows; the bar chart draws.
      await expect(tiles(page).nth(0)).toContainText(/\d/);
      await expect(tiles(page).nth(1).locator("canvas")).toBeVisible();
    });

    await test.step("save and reload", async () => {
      await page.getByRole("button", { name: "Save", exact: true }).click();
      await expect(status).toHaveText("Saved as version 2");
      await page.reload();
      await expect(tiles(page)).toHaveCount(2);
      await expect(status).toHaveText("Saved as version 2");
    });

    await test.step("undo, then restore an old version", async () => {
      await tiles(page).nth(1).getByRole("button", { name: /^Select visual/ }).click();
      await page.getByRole("button", { name: "Remove this visual" }).click();
      await expect(tiles(page)).toHaveCount(1);
      await page.getByRole("button", { name: "Undo", exact: true }).click();
      await expect(tiles(page)).toHaveCount(2);
      await page.getByRole("button", { name: "Redo", exact: true }).click();
      await expect(tiles(page)).toHaveCount(1);
      await page.getByRole("button", { name: "Save", exact: true }).click();
      await expect(status).toHaveText("Saved as version 3");

      await page.getByRole("tab", { name: "History", exact: true }).click();
      await page.getByRole("button", { name: /^Version 2/ }).click();
      await expect(page.getByLabel("Changes since version 2")).toBeVisible();
      await page.getByRole("button", { name: "Restore version 2" }).click();
      await expect(status).toHaveText("Saved as version 4");
      await expect(tiles(page)).toHaveCount(2);
    });

    await test.step("the JSON tab refuses a broken spec with its path", async () => {
      await page.getByRole("tab", { name: "JSON", exact: true }).click();
      const editor = page.locator(".monaco-editor").first();
      await expect(editor).toBeVisible({ timeout: 30_000 });
      await editor.click();
      await page.keyboard.press("ControlOrMeta+A");
      await page.keyboard.press("Delete");
      await page.keyboard.insertText(' {"specVersion":1,"title":"x","layout":{"columns":12,"rowHeight":40},"filters":[],"items":[],"interactions":{"crossFilter":"yes"}}');
      await page.getByRole("button", { name: "Apply", exact: true }).click();
      await expect(page.getByText("/interactions/crossFilter")).toBeVisible();
      await expect(tiles(page)).toHaveCount(2);
      await page.getByRole("button", { name: "Discard edits" }).click();
    });

    await test.step("publish", async () => {
      await page.getByRole("button", { name: "Publish", exact: true }).click();
      await expect(page.getByText("Published", { exact: true })).toBeVisible();
      await page.goto(`dashboards/${dashboards[0]}`);
      await expect(page.getByText("Published version")).toBeVisible();
      await expect(tiles(page)).toHaveCount(2);
      await expect(tiles(page).nth(1).locator("canvas")).toBeVisible();
    });

    expect(problems).toEqual([]);
  } finally {
    await removeFixture(page, fixture, dashboards);
  }
});
