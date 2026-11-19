// Before the tests: when the stack asks people to sign in, sign in as the first
// admin (choosing a new password if this is the first sign-in) and keep the
// session for every test. Without sign-in this writes an empty state.
import { request, type FullConfig } from "@playwright/test";
import { mkdirSync, writeFileSync } from "node:fs";
import { dirname } from "node:path";

export const STATE = ".auth/admin.json";

export default async function globalSetup(config: FullConfig): Promise<void> {
  const baseURL = config.projects[0]!.use.baseURL!;
  mkdirSync(dirname(STATE), { recursive: true });
  const context = await request.newContext({ baseURL });
  const settings = await (await context.get("api/config")).json();
  if (settings.authMode === "none") {
    writeFileSync(STATE, JSON.stringify({ cookies: [], origins: [] }));
    await context.dispose();
    return;
  }
  const email = process.env.OPSIS_ADMIN_EMAIL;
  const initial = process.env.OPSIS_ADMIN_PASSWORD;
  const chosen = process.env.OPSIS_ADMIN_NEW_PASSWORD;
  if (!email || !initial || !chosen) {
    throw new Error("This stack needs sign-in: set OPSIS_ADMIN_EMAIL, OPSIS_ADMIN_PASSWORD and OPSIS_ADMIN_NEW_PASSWORD.");
  }
  const csrf = async () => {
    const cookies = (await context.storageState()).cookies;
    return { "X-CSRF-Token": decodeURIComponent(cookies.find((c) => c.name === "opsis_csrf")!.value) };
  };
  let signed = await context.post("api/auth/login", { headers: await csrf(), data: { email, password: chosen } });
  if (signed.status() === 401) {
    signed = await context.post("api/auth/login", { headers: await csrf(), data: { email, password: initial } });
    if (!signed.ok()) throw new Error(`Admin sign-in failed: ${signed.status()} ${await signed.text()}`);
    const changed = await context.post("api/auth/password", {
      headers: await csrf(),
      data: { currentPassword: initial, newPassword: chosen },
    });
    if (!changed.ok()) throw new Error(`Password change failed: ${changed.status()} ${await changed.text()}`);
  } else if (!signed.ok()) {
    throw new Error(`Admin sign-in failed: ${signed.status()} ${await signed.text()}`);
  }
  await context.storageState({ path: STATE });
  await context.dispose();
}
