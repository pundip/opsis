// Warnings on a semantic model: a join to a dataset that lists a key twice, and
// to one with row-level security, are shown on Check, on Save and on the model
// page, and the data is checked again after the joined dataset refreshes.
import { expect, test } from "@playwright/test";
import {
  type Fixture,
  api,
  createDataset,
  refreshDataset,
  removeFixture,
  uploadCsv,
  watchPage,
} from "./helpers";

// The lookup lists Library twice, then only once.
const TWICE = ["fund,category", "Library,Facilities", "Library,Buildings", "Sport,Community"].join("\n");
const ONCE = ["fund,category", "Library,Facilities", "Sport,Community"].join("\n");

test("join warnings on a model: repeated keys and rules that do not carry over", async ({ page }) => {
  const problems = watchPage(page);
  let gifts: Fixture | undefined;
  let funds: Fixture | undefined;
  try {
    await page.goto("models");
    gifts = await createDataset(page);
    funds = await createDataset(page, TWICE);
    // The lookup protects its own rows. The gifts model has no rule of its own.
    await api(page, "PUT", `api/models/${funds.dataset}`, {
      definition: {
        dimensions: { fund: { column: "fund", type: "string" } },
        rls: [{ filter: "category IN {{ user.attributes.categories }}" }],
      },
    });

    await page.goto(`models/${gifts.dataset}`);
    const editor = page.getByLabel("Model definition in YAML");
    const checked = page.getByRole("list", { name: "Warnings from the check" });
    const shown = page.getByRole("list", { name: "Model warnings" });

    await test.step("Check lists both warnings and still calls the model valid", async () => {
      await expect(editor).toHaveValue(/dimensions:/);
      await expect(shown).toHaveCount(0);
      const yaml = await editor.inputValue();
      await editor.fill(
        `${yaml.trimEnd()}\njoins:\n  - to: ${funds!.dataset}\n    on: {fund: fund}\n    relationship: many_to_one\n`,
      );
      await page.getByRole("button", { name: "Check", exact: true }).click();
      await expect(page.getByText("The model is valid. Save it to use it in queries.")).toBeVisible();
      await expect(checked.getByRole("listitem")).toHaveCount(2);
      await expect(checked).toContainText(`its key 'fund' is not unique in '${funds!.dataset}'`);
      await expect(checked).toContainText("1 value is on more than one row, for example 'Library' (2 rows)");
      await expect(checked).toContainText(
        `'${funds!.dataset}' has row-level security but '${gifts!.dataset}' has none`,
      );
      await expect(shown).toHaveCount(0); // nothing is saved yet
    });

    await test.step("Save is not blocked, and the model page shows the warnings", async () => {
      await page.getByRole("button", { name: "Save model" }).click();
      await expect(page.getByText("Saved.", { exact: true })).toBeVisible();
      await expect(checked.getByRole("listitem")).toHaveCount(2);
      await expect(page.getByRole("heading", { name: "2 warnings" })).toBeVisible();
      await page.reload();
      await expect(shown.getByRole("listitem")).toHaveCount(2);
      await expect(shown).toContainText("for example 'Library' (2 rows)");
      // What the warning is about: Library's total is counted twice.
      const totals = await api(page, "POST", "api/query", {
        query: {
          dataset: gifts!.dataset,
          measures: [`${gifts!.dataset}.total_amount`],
          dimensions: [`${funds!.dataset}.fund`],
        },
      });
      expect(Object.fromEntries(totals.rows)).toEqual({ Library: 600, Sport: 700 });
    });

    await test.step("after the lookup is corrected and refreshed, only the rules remain", async () => {
      const upload = await uploadCsv(page, `smoke_${funds!.stamp}.csv`, ONCE);
      await api(page, "PATCH", `api/connections/${funds!.connectionId}`, {
        config: { file: upload.file },
      });
      await refreshDataset(page, funds!.datasetId);
      await page.reload();
      await expect(page.getByRole("heading", { name: "1 warning" })).toBeVisible();
      await expect(shown).toContainText("has row-level security");
      await expect(shown).not.toContainText("not unique");
    });
    expect(problems).toEqual([]);
  } finally {
    await removeFixture(page, gifts, []);
    await removeFixture(page, funds, []);
  }
});
