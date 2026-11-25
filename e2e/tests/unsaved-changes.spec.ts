// Leaving the editor with unsaved changes asks first: by a breadcrumb, the menu, the
// Back button or a reload. Stay keeps the work; leave drops it.
import { expect, test } from "@playwright/test";
import { api, tiles, watchPage } from "./helpers";

test("leaving the editor with unsaved changes asks first", async ({ page }) => {
  const problems = watchPage(page);
  await page.goto("dashboards");
  const dashboard = await api(page, "POST", "api/dashboards", { title: `Leaving ${Date.now().toString(36)}` });
  const view = `dashboards/${dashboard.id}`;
  const editor = `${view}/edit`;
  const status = page.getByRole("status").filter({ hasText: /Saved as version|Unsaved changes/ });
  const question = page.getByRole("alertdialog", { name: "Leave without saving?" });
  const stay = question.getByRole("button", { name: "Stay", exact: true });
  const leave = question.getByRole("button", { name: "Leave without saving", exact: true });
  const crumb = (name: string) => page.getByRole("main").getByRole("link", { name, exact: true });
  const menu = (name: string) => page.getByRole("navigation", { name: "Main" }).getByRole("link", { name, exact: true });
  const save = page.getByRole("button", { name: "Save", exact: true });

  /** Into the editor through the app, so that Back has a page of the app to go to. */
  const edit = async () => {
    await page.goto(view);
    await page.getByRole("link", { name: "Edit", exact: true }).click();
    await expect(page).toHaveURL(editor);
  };
  const change = async () => {
    await page.getByRole("toolbar", { name: "Add a visual" }).getByRole("button", { name: "Text", exact: true }).click();
    await expect(status).toHaveText("Unsaved changes");
  };

  try {
    await test.step("nothing to lose, no question", async () => {
      await edit();
      await crumb("View").click();
      await expect(page).toHaveURL(view);
      await expect(question).toHaveCount(0);
    });

    await test.step("stay keeps the page and the work", async () => {
      await edit();
      await change();
      for (const link of [crumb("Dashboards"), crumb("View"), menu("Datasets")]) {
        await link.click();
        await expect(question).toBeVisible();
        await expect(stay).toBeFocused();
        await expect(page).toHaveURL(editor);
        await stay.click();
        await expect(question).toHaveCount(0);
      }
      // Escape means stay.
      await menu("Home").click();
      await expect(question).toBeVisible();
      await page.keyboard.press("Escape");
      await expect(question).toHaveCount(0);
      // A way out taken in the same instant as the answer is asked about like any other.
      const next = (await crumb("Dashboards").elementHandle())!;
      await menu("Datasets").click();
      await expect(question).toBeVisible();
      await page.evaluate(
        ([answer, link]) => {
          (answer as HTMLElement).click();
          (link as HTMLElement).click();
        },
        [(await stay.elementHandle())!, next],
      );
      await expect(question).toBeVisible();
      await expect(page).toHaveURL(editor);
      await stay.click();
      await expect(question).toHaveCount(0);
      // The Back button asks too.
      await page.goBack();
      await expect(question).toBeVisible();
      await stay.click();
      await expect(question).toHaveCount(0);
      // The editor's own shortcuts wait while the question is open.
      await menu("Datasets").click();
      await expect(question).toBeVisible();
      await page.keyboard.press("ControlOrMeta+Z");
      await stay.click();
      await expect(page).toHaveURL(editor);
      await expect(tiles(page)).toHaveCount(1);
      await expect(status).toHaveText("Unsaved changes");
    });

    await test.step("a reload still gets the browser's warning", async () => {
      const asked = page.waitForEvent("dialog");
      await page.evaluate(() => void setTimeout(() => window.location.reload(), 0));
      const warning = await asked;
      expect(warning.type()).toBe("beforeunload");
      await warning.dismiss();
      await expect(tiles(page)).toHaveCount(1);
    });

    await test.step("leave goes where the link pointed and saves nothing", async () => {
      await crumb("Dashboards").click();
      await leave.click();
      await expect(page).toHaveURL("dashboards");
      await expect(page.getByRole("heading", { name: "Dashboards", exact: true })).toBeVisible();
      expect((await api(page, "GET", `api/dashboards/${dashboard.id}`)).spec.items).toEqual([]);

      await edit();
      await expect(tiles(page)).toHaveCount(0);
      await change();
      await page.goBack();
      await leave.click();
      await expect(page).toHaveURL(view);
      await expect(question).toHaveCount(0);

      await edit();
      await change();
      await menu("Datasets").click();
      await leave.click();
      await expect(page).toHaveURL("datasets");
    });

    await test.step("a save that finishes while the question is open lets you go", async () => {
      await edit();
      await change();
      let release = () => {};
      const held = new Promise<void>((resolve) => (release = resolve));
      await page.route(`**/api/dashboards/${dashboard.id}`, async (route) => {
        if (route.request().method() === "PUT") await held;
        await route.continue();
      });
      await save.click();
      await menu("Datasets").click();
      await expect(question).toBeVisible();
      release();
      await expect(page).toHaveURL("datasets");
      await expect(question).toHaveCount(0);
      expect((await api(page, "GET", `api/dashboards/${dashboard.id}`)).spec.items).toHaveLength(1);
    });

    await test.step("after saving there is nothing to ask", async () => {
      await edit();
      await change();
      await save.click();
      await expect(status).toHaveText("Saved as version 3");
      await menu("Datasets").click();
      await expect(page).toHaveURL("datasets");
      await expect(question).toHaveCount(0);
    });

    expect(problems).toEqual([]);
  } finally {
    await api(page, "DELETE", `api/dashboards/${dashboard.id}`);
  }
});
