// The user guide inside the app (docs/guide, rendered by the api, at
// {base}/docs/guide): the Help link, tables and code, links between chapters and
// to headings, screenshots, and who may read it. Everything stays inside the app,
// so it works offline. Runs at the root, the sub-path and in server mode.
import { expect, test, type Page } from "@playwright/test";
import { baseURL, serverMode } from "../playwright.config";
import { watchPage } from "./helpers";

/** "" at the root, "/opsis" behind the proxy: the prefix every address carries. */
const base = new URL(baseURL).pathname.replace(/\/$/, "");
const article = (page: Page) => page.locator("article.guide");
const heading = (page: Page, name: string) => article(page).getByRole("heading", { name, exact: true });

/** Follow every link of the guide from its contents page and load every screenshot. */
function crawl(page: Page): Promise<{ pages: number; images: number; broken: string[] }> {
  return page.evaluate(async (base) => {
    const prefix = `${base}/docs/guide`;
    const pages = new Map<string, Document>();
    const images = new Set<string>();
    const anchors: [from: string, to: string, anchor: string][] = [];
    const broken: string[] = [];
    const queue = [""]; // the contents page
    for (let next = queue.shift(); next !== undefined; next = queue.shift()) {
      if (pages.has(next)) continue;
      const response = await fetch(`${base}/api/guide${next && `/${next}`}`);
      if (!response.ok) {
        broken.push(`page "${next}": ${response.status}`);
        continue;
      }
      const parsed = new DOMParser().parseFromString((await response.json()).html, "text/html");
      pages.set(next, parsed);
      for (const image of parsed.querySelectorAll("img")) images.add(image.getAttribute("src")!);
      for (const link of parsed.querySelectorAll("a")) {
        const href = link.getAttribute("href")!;
        if (/^(https?:|mailto:)/.test(href)) continue;
        const [path = "", anchor] = href.split("#");
        if (path !== prefix && !path.startsWith(`${prefix}/`)) {
          broken.push(`${next}: ${href} is outside the guide`);
          continue;
        }
        const target = path.slice(prefix.length + 1);
        queue.push(target);
        if (anchor) anchors.push([next, target, anchor]);
      }
    }
    for (const [from, to, anchor] of anchors) {
      if (!pages.get(to)?.getElementById(`guide-${anchor}`)) broken.push(`${from}: no heading ${to}#${anchor}`);
    }
    for (const src of images) {
      const response = await fetch(src);
      const type = response.headers.get("content-type") ?? "";
      if (!src.startsWith(`${base}/api/guide/images/`) || !response.ok || !type.startsWith("image/")) {
        broken.push(`image ${src}: ${response.status} ${type}`);
      }
    }
    return { pages: pages.size, images: images.size, broken };
  }, base);
}

test("the user guide reads inside the app, with its links and screenshots", async ({ page }) => {
  const problems = watchPage(page);
  const elsewhere: string[] = [];
  page.on("request", (request) => {
    // Nothing from a CDN, and nothing outside the base path.
    if (!/^(data|blob):/.test(request.url()) && !request.url().startsWith(baseURL)) elsewhere.push(request.url());
  });

  await test.step("Help in the menu opens the contents, with its table and diagram", async () => {
    await page.goto("");
    const help = page.getByRole("navigation", { name: "Main" }).getByRole("link", { name: "Help", exact: true });
    await help.click();
    await expect(page).toHaveURL(`${baseURL}docs/guide`);
    await expect(help).toHaveAttribute("aria-current", "page");
    await expect(article(page).getByRole("heading", { level: 1, name: "Opsis user guide" })).toBeVisible();
    await expect(article(page).getByRole("cell", { name: "1. Getting started", exact: true })).toBeVisible();
    await expect(article(page).locator("pre code").first()).toContainText("Connection");
    // The deployment guides are not part of it: named, not linked.
    await expect(article(page)).toContainText("Windows guide (deploy/windows/README.md)");
    await expect(article(page).getByRole("link", { name: "Windows guide" })).toHaveCount(0);
  });

  await test.step("every page, heading link and screenshot is there", async () => {
    const found = await crawl(page);
    expect(found.broken).toEqual([]);
    expect(found.pages).toBeGreaterThanOrEqual(10);
    expect(found.images).toBeGreaterThanOrEqual(20);
  });

  await test.step("a link goes to a heading in another chapter, without reloading the app", async () => {
    await page.evaluate(() => Object.assign(window, { sameDocument: true }));
    await article(page).getByRole("link", { name: "Build a dashboard", exact: true }).click();
    await expect(page).toHaveURL(`${baseURL}docs/guide/06-dashboards#a-worked-example`);
    await expect(heading(page, "A worked example")).toBeInViewport();
    expect(await page.evaluate(() => "sameDocument" in window)).toBe(true);
    const images = article(page).locator("img");
    expect(await images.count()).toBeGreaterThan(0);
    for (const image of await images.all()) {
      await image.scrollIntoViewIfNeeded();
      await expect.poll(() => image.evaluate((element: HTMLImageElement) => element.naturalWidth)).toBeGreaterThan(0);
    }
  });

  await test.step("the next chapter starts at its top, and Back returns to the same place", async () => {
    const next = article(page).getByRole("link", { name: "Sharing and security", exact: true }).last();
    await next.scrollIntoViewIfNeeded();
    const left = await page.evaluate(() => window.scrollY);
    expect(left).toBeGreaterThan(500);
    await next.click();
    await expect(page).toHaveURL(`${baseURL}docs/guide/07-sharing-and-security`);
    await expect(heading(page, "7. Sharing and security")).toBeInViewport();
    expect(await page.evaluate(() => window.scrollY)).toBe(0);
    await page.goBack();
    await expect(page).toHaveURL(`${baseURL}docs/guide/06-dashboards#a-worked-example`);
    await expect.poll(() => page.evaluate((y) => Math.abs(window.scrollY - y), left)).toBeLessThan(5);
    await page.goForward();
    await expect(heading(page, "7. Sharing and security")).toBeInViewport();
  });

  await test.step("a link within a page, and Back", async () => {
    await article(page).getByRole("link", { name: "row-level security", exact: true }).click();
    await expect(page).toHaveURL(`${baseURL}docs/guide/07-sharing-and-security#row-level-security`);
    await expect(heading(page, "Row-level security")).toBeInViewport();
    await page.goBack();
    await expect(page).toHaveURL(`${baseURL}docs/guide/07-sharing-and-security`);
    await expect(heading(page, "7. Sharing and security")).toBeInViewport();
    expect(await page.evaluate(() => "sameDocument" in window)).toBe(true);
  });

  await test.step("a deep link opens at its heading, also after a reload", async () => {
    await page.goto("docs/guide/06-dashboards#the-json-tab");
    await expect(heading(page, "The JSON tab")).toBeInViewport();
    // The screenshots above it have their room kept, so it stays there as they load.
    await page.waitForLoadState("networkidle");
    expect((await heading(page, "The JSON tab").boundingBox())!.y).toBeLessThan(100);
    await page.reload();
    await expect(heading(page, "The JSON tab")).toBeInViewport();
  });

  await test.step("a page that is not there says so; the API reference is untouched", async () => {
    await page.goto("docs/guide/nope");
    await expect(page.getByRole("alert")).toHaveText("There is no such page in the user guide.");
    await page.getByRole("link", { name: "Guide contents" }).click();
    await expect(heading(page, "Opsis user guide")).toBeVisible();
    const reference = await page.request.get("api/docs");
    expect(await reference.text()).toContain("swagger-ui");
  });

  expect(problems).toEqual([]);
  expect(elsewhere).toEqual([]);
});

test("with sign-in, only signed-in people read the guide", async ({ browser }) => {
  test.skip(!serverMode, "Local mode has no sign-in: the guide is simply open.");
  const context = await browser.newContext({ storageState: { cookies: [], origins: [] }, viewport: { width: 1500, height: 950 } });
  const page = await context.newPage();
  await page.goto("docs/guide/05-joins#two-joins-away");
  await expect(page.getByRole("heading", { name: "Sign in" })).toBeVisible();
  await expect(article(page)).toHaveCount(0);
  for (const path of ["api/guide", "api/guide/05-joins", "api/guide/images/dashboard-demo.png"]) {
    expect((await page.request.get(path)).status(), path).toBe(401);
  }
  // Signing in shows the page that was asked for.
  await page.getByLabel("Email", { exact: true }).fill(process.env.OPSIS_ADMIN_EMAIL!);
  await page.getByLabel("Password", { exact: true }).fill(process.env.OPSIS_ADMIN_NEW_PASSWORD!);
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(heading(page, "Two joins away")).toBeInViewport();
  expect((await page.request.get("api/guide/images/dashboard-demo.png")).status()).toBe(200);
  await context.close();
});
