// M8: a dataset's refresh schedule, the Schedules page and the Settings page.
// The smoke stacks run with schedules and backups on (compose.fakellm.yml at the
// root and the sub-path, compose.servertest.yml in server mode). Only the
// disposable server-mode stack makes a real backup, in its worker.
import { expect, test } from "@playwright/test";
import { serverMode } from "../playwright.config";
import { type Fixture, createDataset, removeFixture, watchPage } from "./helpers";

test("a dataset refreshed on a schedule; the Schedules and Settings pages", async ({ page }) => {
  const problems = watchPage(page);
  let fixture: Fixture | undefined;
  try {
    await page.goto("datasets");
    fixture = await createDataset(page);

    await test.step("a preset shows its next runs and is saved", async () => {
      await page.goto(`datasets/${fixture!.datasetId}`);
      const card = page.locator("section").filter({ has: page.getByRole("heading", { name: "Refresh schedule" }) });
      await expect(card.getByText(/^Not refreshed on a schedule/)).toBeVisible();
      await card.getByLabel("When").selectOption({ label: "Weekdays at 07:00" });
      await expect(card.getByRole("status")).toContainText("Next runs:");
      await card.getByRole("button", { name: "Save schedule" }).click();
      await expect(card.getByText(/^Next refresh /)).toBeVisible();

      // A cron expression that runs too often is refused before it can be saved.
      await card.getByLabel("When").selectOption({ label: "Custom (cron)" });
      await card.getByLabel("Cron expression").fill("*/5 * * * *");
      await expect(card.getByRole("status")).toContainText("too often");
      await expect(card.getByRole("button", { name: "Save schedule" })).toBeDisabled();
    });

    await test.step("the Schedules page lists it, with the backup and the clean-up", async () => {
      await page.goto("schedules");
      await expect(page.getByRole("heading", { name: "Schedules" })).toBeVisible();
      const row = page.getByRole("row").filter({ hasText: new RegExp(fixture!.stamp, "i") });
      await expect(row).toContainText("0 7 * * 1-5");
      await expect(page.getByRole("cell", { name: "Backup", exact: true })).toBeVisible();
      await expect(page.getByRole("cell", { name: "Clean-up of ended sessions and links" })).toBeVisible();
    });

    await test.step("Settings shows the backups and this install's features", async () => {
      await page.goto("settings");
      await expect(page.getByRole("heading", { name: "Backups" })).toBeVisible();
      await expect(page.getByText(/^Next backup /)).toBeVisible();
      await expect(page.getByRole("button", { name: "Back up now" })).toBeEnabled();
      if (serverMode) {
        // The worker makes a real backup: pg_dump, the uploads and the current data.
        await page.getByRole("button", { name: "Back up now" }).click();
        await expect(page.getByRole("cell", { name: /^opsis-\d{8}-\d{6}\.tar$/ })).toBeVisible({ timeout: 90_000 });
      }
    });

    if (serverMode) {
      // Only on the disposable stack: your own stack gets no demo data from the tests.
      await test.step("the demo data loads, with a dashboard that has data", async () => {
        await page.goto("settings");
        await page.getByRole("button", { name: "Load demo data" }).click();
        await page.getByRole("link", { name: "Open the demo dashboard" }).click({ timeout: 90_000 });
        await expect(page.getByRole("heading", { name: "Giving overview (demo)" })).toBeVisible();
        await expect(page.locator("[data-item]")).toHaveCount(8);
        await expect(page.locator("[data-item]").first()).toContainText("$");
        await expect(page.locator("[data-item]").last()).toContainText("Victoria");
      });
    }
  } finally {
    await removeFixture(page, fixture, []);
  }
  expect(problems).toEqual([]);
});
