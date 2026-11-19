// Local mode (AUTH_MODE=none) exposes none of the server features: no sign-in, no
// people or groups, no sharing and no share links (brief M6).
import { expect, test } from "@playwright/test";
import { serverMode } from "../playwright.config";
import { api } from "./helpers";

test.skip(serverMode, "Local mode only.");

test("local mode has no sign-in, people, sharing or share links", async ({ page }) => {
  await page.goto("dashboards");
  const config = await api(page, "GET", "api/config");
  expect(config.authMode).toBe("none");
  for (const path of ["api/auth/links/x", "api/users", "api/groups", "api/directory", "api/public/links/x"]) {
    expect((await page.request.get(path)).status(), path).toBe(404);
  }
  await expect(page.getByRole("button", { name: "Sign out" })).toHaveCount(0);
  await expect(page.getByRole("link", { name: "Users" })).toHaveCount(0);
  const dashboard = await api(page, "POST", "api/dashboards", { title: `Local ${Date.now()}` });
  try {
    await page.goto(`dashboards/${dashboard.id}`);
    await expect(page.getByRole("heading", { name: dashboard.title })).toBeVisible();
    await expect(page.getByRole("button", { name: "Share" })).toHaveCount(0);
  } finally {
    await api(page, "DELETE", `api/dashboards/${dashboard.id}`);
  }
});
