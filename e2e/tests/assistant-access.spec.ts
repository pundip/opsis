// Server mode, bring your own agent (v2 V1): someone with no key and no grant is told
// how to get one; two editors each build with their own key (Anthropic and OpenAI),
// and each key answers only its owner's requests; an admin allows one of them the
// workspace's Claude key on the Users page, then takes it away, which stops the
// very next request without a reload. Runs only against the disposable server-mode
// stack (OPSIS_MODE=server), which is removed afterwards.
import { expect, test } from "@playwright/test";
import { serverMode } from "../playwright.config";
import { api, askToBuild, createDataset, invitePerson, watchPage } from "./helpers";

test.skip(!serverMode || !process.env.OPSIS_FAKE_MODEL, "Needs the server-mode stack and the fake model: npm run smoke runs it.");

test("own keys answer for their owners; the workspace key is granted and taken away", async ({ page, browser }) => {
  const stamp = Date.now().toString(36);
  const annKey = `sk-ant-api03-ann-${stamp}-${"A".repeat(24)}aNn1`;
  const bobKey = `sk-proj-bob-${stamp}-${"B".repeat(24)}bOb2`;
  const workspace = `key ending ${process.env.OPSIS_WORKSPACE_KEY_END ?? ""}`;
  await page.goto("dashboards");
  const fixture = await createDataset(page);
  const dashboard = await api(page, "POST", "api/dashboards", { title: `Keys ${stamp}` });
  await api(page, "PUT", `api/dashboards/${dashboard.id}/access`, { shares: [{ grantee: "everyone", role: "editor" }] });
  const ann = await invitePerson(page, browser, "Ann");
  const bob = await invitePerson(page, browser, "Bob");
  const problems = [...[ann.page, bob.page].map((p) => watchPage(p))];
  const editor = async (who: typeof ann) => {
    await who.page.goto(`dashboards/${dashboard.id}/edit`);
    await who.page.getByRole("tab", { name: "Assistant" }).click();
  };

  await test.step("no key and no grant: the panel says how to get one", async () => {
    await editor(ann);
    const note = ann.page.getByRole("note").filter({ hasText: "No AI provider to use yet" });
    await expect(note).toContainText("Add your own Anthropic or OpenAI API key on your Account page, or ask an admin to let you use the workspace's key.");
    await ann.page.getByLabel("Message to the assistant").fill("Hello");
    await expect(ann.page.getByRole("button", { name: "Send", exact: true })).toBeDisabled();
    await note.getByRole("link", { name: "Add a key on your Account page" }).click();
    await expect(ann.page.getByRole("heading", { name: "AI providers" })).toBeVisible();
  });

  await test.step("Ann adds her Anthropic key and builds with it", async () => {
    await ann.page.getByLabel("Provider", { exact: true }).selectOption("anthropic");
    await ann.page.getByLabel("Name", { exact: true }).fill("Ann's Claude");
    await ann.page.getByLabel("API key", { exact: true }).fill(annKey);
    await ann.page.getByRole("button", { name: "Add key", exact: true }).click();
    await expect(ann.page.getByRole("status").filter({ hasText: "The key works" })).toBeVisible();
    await editor(ann);
    expect(await askToBuild(ann.page, fixture.dataset)).toContain(`key ending ${annKey.slice(-4)}`);
  });

  await test.step("Bob builds with his OpenAI key, never with Ann's", async () => {
    await api(bob.page, "POST", "api/account/api-keys", { provider: "openai", label: "Bob's OpenAI", key: bobKey, defaultModel: "gpt-5.5" });
    await editor(bob);
    const reply = await askToBuild(bob.page, fixture.dataset);
    expect(reply).toContain(`key ending ${bobKey.slice(-4)}`);
    expect(reply).not.toContain(annKey.slice(-4));
  });

  await test.step("an admin allows Bob the workspace key; he builds with it", async () => {
    await page.goto("users");
    await page.getByRole("tab", { name: "AI assistant" }).click();
    const table = page.getByRole("table", { name: "Who may use the workspace key" });
    const row = table.getByRole("row", { name: new RegExp(bob.email) });
    await expect(row).toContainText("OpenAI 1");
    await expect(row).toContainText("No");
    await row.getByLabel(`${bob.email} may use the workspace key`).check();
    await expect(row).toContainText("Yes");
    expect(await page.content()).not.toContain(bobKey);
    await editor(bob);
    await bob.page.getByLabel("AI provider").selectOption({ label: "The workspace's Claude key" });
    expect(await askToBuild(bob.page, fixture.dataset)).toContain(workspace);
  });

  await test.step("taken away, it stops Bob's very next request, with no reload", async () => {
    const row = page.getByRole("table", { name: "Who may use the workspace key" }).getByRole("row", { name: new RegExp(bob.email) });
    await row.getByLabel(`${bob.email} may use the workspace key`).uncheck();
    await expect(row.getByText("No", { exact: true })).toBeVisible();
    await bob.page.getByLabel("Message to the assistant").fill(`Build a dashboard from ${fixture.dataset}`);
    await bob.page.getByRole("button", { name: "Send", exact: true }).click();
    await expect(bob.page.getByRole("alert").filter({ hasText: "You may no longer use the workspace's key. Choose what the assistant should use." }).first()).toBeVisible();
    // Nothing else is used behind his back: he chooses his own key again.
    await expect(bob.page.getByLabel("AI provider")).toHaveValue("");
    await bob.page.getByLabel("AI provider").selectOption({ label: `Bob's OpenAI (your OpenAI key ending ${bobKey.slice(-4)})` });
    expect(await askToBuild(bob.page, fixture.dataset)).toContain(`key ending ${bobKey.slice(-4)}`);
  });

  for (const found of problems) expect(found).toEqual([]);
});
