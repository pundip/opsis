// Legacy data on the dataset page: Opsis suggests types for text columns, a
// person sets column types by hand, and the refresh report lists the values
// stored as NULL with the reason and samples.
import { expect, test } from "@playwright/test";
import { type Fixture, api, createDataset, removeFixture, watchPage } from "./helpers";

// DuckDB reads every column here as text: "n/a" is not a number, "0000-00-00"
// is not a date, and it does not read month names.
const SLOPPY = [
  "id,amount,given,ended",
  "1,12,1 Jan 2020,2020-01-01",
  "2,n/a,2 Feb 2020,0000-00-00",
  "3,7,3 Mar 2021,2020-01-03",
  "4,15,4 Apr 2022,2020-01-04",
  "5,20,5 May 2023,2020-01-05",
  "6,30,6 Jun 2024,2020-01-06",
].join("\n");

test("suggested and chosen column types, and the report of values stored as NULL", async ({ page }) => {
  const problems = watchPage(page);
  let fixture: Fixture | undefined;
  try {
    await page.goto("datasets");
    fixture = await createDataset(page, SLOPPY);
    await page.goto(`datasets/${fixture.datasetId}`);

    const quality = page.locator("section").filter({ has: page.getByRole("heading", { name: "Data quality" }) });
    const types = page.locator("section").filter({ has: page.getByRole("heading", { name: "Column types" }) });

    await test.step("text columns that hold numbers and dates are suggested a type, not given one", async () => {
      await expect(quality).toContainText("3 text columns look like another type");
      await expect(quality).toContainText("amount (whole number)");
      await expect(types.getByText("Looks like date written %d %b %Y")).toBeVisible();
      await expect(types.getByRole("button", { name: "Save and rebuild" })).toBeDisabled();
    });

    await test.step("a suggestion is taken, another type is chosen by hand, and the dataset is rebuilt", async () => {
      await types.getByRole("button", { name: "Use the suggested type for amount" }).click();
      await types.getByRole("button", { name: "Use the suggested type for given" }).click();
      await expect(types.getByLabel("Date format of given")).toHaveValue("%d %b %Y");
      await types.getByLabel("Type of ended").selectOption({ label: "Date" });
      await types.getByRole("button", { name: "Save and rebuild" }).click();
      await expect(types.getByRole("status")).toContainText("being rebuilt");
      await expect
        .poll(async () => (await api(page, "GET", `api/datasets/${fixture!.datasetId}`)).lastJob?.status, {
          timeout: 60_000,
        })
        .toBe("succeeded");
      await page.reload();
    });

    await test.step("the report names each column, the reason and the raw values", async () => {
      const table = quality.getByRole("table", { name: "Values stored as NULL" });
      await expect(quality).toContainText("stored 2 values as empty (NULL)");
      const ended = table.getByRole("row").filter({ hasText: "ended" });
      await expect(ended).toContainText("zero date");
      await expect(ended).toContainText("0000-00-00");
      const amount = table.getByRole("row").filter({ hasText: "amount" });
      await expect(amount).toContainText("not a number");
      await expect(amount).toContainText("n/a");
      await expect(types.getByText("Set by hand")).toHaveCount(3);
      // The preview shows the new types.
      await expect(page.getByRole("columnheader").filter({ hasText: /^ended/ })).toContainText("date");
      await expect(page.getByRole("columnheader").filter({ hasText: /^amount/ })).toContainText("integer");
      // So does the refresh history.
      await expect(page.getByText("2 set to NULL").first()).toBeVisible();
    });

    await test.step("going back to the source's types", async () => {
      for (const name of ["amount", "given", "ended"]) {
        await types.getByLabel(`Type of ${name}`).selectOption({ label: "As the source gives it" });
      }
      await types.getByRole("button", { name: "Save and rebuild" }).click();
      await expect(types.getByRole("status")).toContainText("being rebuilt");
      await expect
        .poll(
          async () => {
            const found = await api(page, "GET", `api/datasets/${fixture!.datasetId}`);
            return [found.lastJob?.status, found.columnOverrides];
          },
          { timeout: 60_000 },
        )
        .toEqual(["succeeded", {}]);
    });
    expect(problems).toEqual([]);
  } finally {
    await removeFixture(page, fixture, []);
  }
});
