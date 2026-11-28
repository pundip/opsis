// V2: the Model page. Two tables related by dragging a field of one onto a field of
// the other, a visual that uses fields of both, and an edit in the YAML that the
// canvas then shows.
import { type Locator, type Page, expect, test } from "@playwright/test";
import { type Fixture, api, createDataset, dashboardIdFrom, removeFixture, tiles, watchPage } from "./helpers";

const GIFTS = ["id,donor_id,amount", "1,1,100", "2,1,200", "3,2,300", "4,3,400"].join("\n");
const DONORS = ["id,name,region", "1,Ann,VIC", "2,Bob,NSW", "3,Cam,VIC"].join("\n");

/** Press on the middle of one element and let go on the middle of another, moving in steps. */
async function drag(page: Page, from: Locator, to: Locator): Promise<void> {
  const start = await from.boundingBox();
  const end = await to.boundingBox();
  if (!start || !end) throw new Error("Both ends of the drag must be on the page.");
  await page.mouse.move(start.x + start.width / 2, start.y + start.height / 2);
  await page.mouse.down();
  await page.mouse.move(end.x + end.width / 2, end.y + end.height / 2, { steps: 12 });
  await page.mouse.up();
}

/** Replace all the text of the focused code editor, as pasting does. Typed text would
 * be indented again by the editor at each new line. */
async function pasteOver(page: Page, text: string): Promise<void> {
  await page.keyboard.press("ControlOrMeta+A");
  await page.evaluate((value) => {
    const data = new DataTransfer();
    data.setData("text/plain", value);
    const event = new ClipboardEvent("paste", { clipboardData: data, bubbles: true, cancelable: true });
    document.activeElement?.dispatchEvent(event);
  }, text);
}

test("the model: relate two tables on the canvas, use both in one visual, edit the YAML", async ({ page }) => {
  const problems = watchPage(page);
  const dashboards: string[] = [];
  let gifts: Fixture | undefined;
  let donors: Fixture | undefined;
  try {
    await page.goto("model");
    gifts = await createDataset(page, GIFTS);
    donors = await createDataset(page, DONORS);
    const id = `${gifts.dataset}__${donors.dataset}`;

    await test.step("drag a field onto a field of another table, check the keys, add the relationship", async () => {
      await page.reload();
      const from = page.getByLabel(`Field ${gifts!.dataset}.donor_id`, { exact: true });
      const to = page.getByLabel(`Field ${donors!.dataset}.id`, { exact: true });
      await expect(from).toBeVisible({ timeout: 30_000 });
      await expect(to).toBeVisible();
      await drag(page, from, to);

      const panel = page.getByRole("complementary", { name: "New relationship" });
      await expect(panel).toContainText("is the many side and");
      await expect(panel).toContainText(`It is saved as ${id}.`);
      await panel.getByRole("button", { name: "Add relationship" }).click();
      await expect(page.getByRole("complementary", { name: `Relationship ${id}` })).toBeVisible();
      await expect(page.getByTestId(`rf__edge-${id}`)).toBeVisible();
      await expect(page.getByRole("button", { name: `Edit relationship ${id}` })).toBeVisible();
      const saved = await api(page, "GET", "api/model");
      expect(saved.document.relationships[id]).toMatchObject({
        from: { table: gifts!.dataset, columns: ["donor_id"] },
        to: { table: donors!.dataset, columns: ["id"] },
      });
    });

    await test.step("a visual uses fields of both tables", async () => {
      await page.goto("dashboards");
      await page.getByLabel("Name of the new dashboard").fill(`Model ${gifts!.stamp}`);
      await page.getByRole("button", { name: "New dashboard" }).click();
      await expect(page).toHaveURL(/dashboards\/[0-9a-f-]{36}\/edit$/);
      dashboards.push(dashboardIdFrom(page.url()));

      await page.getByRole("button", { name: "Table", exact: true }).click();
      await page.getByLabel("Dataset", { exact: true }).selectOption(gifts!.dataset);
      const columns = page.getByLabel("Add a field to Columns", { exact: true });
      await columns.selectOption(`${donors!.dataset}.region`);
      await columns.selectOption(`${gifts!.dataset}.total_amount`);
      const rows = tiles(page).first().getByRole("row");
      await expect(rows.filter({ hasText: "VIC" })).toContainText("700");
      await expect(rows.filter({ hasText: "NSW" })).toContainText("300");

      await page.getByRole("button", { name: "Save", exact: true }).click();
      await expect(page.getByRole("status").filter({ hasText: /Saved as version/ })).toHaveText("Saved as version 2");
      await page.reload();
      await expect(rows.filter({ hasText: "VIC" })).toContainText("700");
    });

    await test.step("an edit in the YAML shows on the canvas", async () => {
      await page.goto("model");
      const { yaml } = await api(page, "GET", "api/model");
      const lines = (yaml as string).split("\n");
      const start = lines.indexOf(`  ${donors!.dataset}:`);
      const label = lines.findIndex((line, index) => index > start && line.startsWith("    label: "));
      expect(start).toBeGreaterThan(-1);
      expect(label).toBeGreaterThan(start);
      lines[label] = "    label: Donor list";

      await page.getByRole("tab", { name: "YAML" }).click();
      const editor = page.getByTestId("model-yaml-editor").locator(".monaco-editor");
      await expect(editor).toBeVisible({ timeout: 30_000 });
      await editor.click();
      await pasteOver(page, lines.join("\n"));
      await page.getByRole("button", { name: "Check", exact: true }).click();
      await expect(page.getByText("The model is valid. Save it to use it.")).toBeVisible();
      await page.getByRole("button", { name: "Save model" }).click();
      await expect(page.getByText(/^Saved as version \d+\.$/)).toBeVisible();

      await page.getByRole("tab", { name: "Canvas" }).click();
      await expect(page.getByTestId(`rf__node-${donors!.dataset}`)).toContainText("Donor list");
      await page.reload();
      await expect(page.getByTestId(`rf__node-${donors!.dataset}`)).toContainText("Donor list");
    });
    expect(problems).toEqual([]);
  } finally {
    await removeFixture(page, undefined, dashboards);
    await removeFixture(page, gifts, []);
    await removeFixture(page, donors, []);
  }
});
