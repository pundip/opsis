// Browser smoke tests in three places:
//   1. at the root, http://localhost:8080 (your stack, AUTH_MODE=none)
//   2. behind the proxytest nginx at a sub-path, http://localhost:8088/opsis/
//   3. server mode at the sub-path: sign-in (local accounts and a fake Google),
//      share links, access tokens for MCP and the REST API, and the worker. This
//      is a separate, disposable stack (project opsis-servertest) with fresh
//      random secrets; it is removed afterwards, data and all.
//
//   npm run smoke                  all three, rebuilding the image first
//   npm run smoke -- --no-build    all three, with the image as it is
//   npm run smoke -- --root        only 1
//   npm run smoke -- --subpath     only 2
//   npm run smoke -- --server      only 3
//
// The assistant answers from a scripted fake model (e2e/fake-llm.mjs): Ollama's
// API at the root and the sub-path (compose.fakellm.yml), Claude's in server mode.
//
// It expects .env to be in plain local mode (PUBLIC_BASE_URL=http://localhost:8080).
// Your stack is left running at the root when it finishes, as your .env has it.
import { spawnSync } from "node:child_process";
import { randomBytes } from "node:crypto";
import { existsSync, rmSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const args = process.argv.slice(2);
const stages = ["--root", "--subpath", "--server"].filter((flag) => args.includes(flag));
const run = (stage) => stages.length === 0 || stages.includes(stage);
const build = args.includes("--no-build") ? [] : ["--build"];
const extra = args.filter((arg) => !["--root", "--subpath", "--server", "--no-build"].includes(arg));

function exec(command, commandArgs, options = {}) {
  console.log(`\n> ${command} ${commandArgs.join(" ")}`);
  const result = spawnSync(command, commandArgs, { cwd: root, stdio: "inherit", shell: process.platform === "win32", ...options });
  return result.status ?? 1;
}

function must(command, commandArgs, options) {
  const status = exec(command, commandArgs, options);
  if (status !== 0) {
    console.error(`\nFailed: ${command} ${commandArgs.join(" ")}`);
    process.exit(status);
  }
}

function playwright(url, env = {}) {
  return exec("npx", ["playwright", "test", ...extra], {
    cwd: join(root, "e2e"),
    env: { ...process.env, OPSIS_URL: url, OPSIS_FAKE_MODEL: "1", ...env },
  });
}

if (!existsSync(join(root, "e2e", "node_modules"))) {
  must("npm", ["install"], { cwd: join(root, "e2e") });
  must("npx", ["playwright", "install", "chromium"], { cwd: join(root, "e2e") });
}

const FAKE = ["-f", "compose.yml", "-f", "compose.fakellm.yml"];
const SUBPATH = [...FAKE, "-f", "compose.subpath.yml", "--profile", "proxytest"];
const SERVER_ENV = join(root, ".env.servertest");
const SERVER = [
  "-p", "opsis-servertest", "--env-file", ".env.servertest",
  "-f", "compose.yml", "-f", "compose.servertest.yml", "--profile", "server", "--profile", "proxytest",
];

const failed = [];
let built = false;
if (run("--root")) {
  must("docker", ["compose", ...FAKE, "up", "-d", ...build, "--wait"]);
  built = true;
  if (playwright("http://localhost:8080/") !== 0) failed.push("root");
}
if (run("--subpath")) {
  must("docker", ["compose", ...SUBPATH, "up", "-d", ...(built ? [] : build), "--wait"]);
  built = true;
  if (playwright("http://localhost:8088/opsis/") !== 0) failed.push("sub-path");
  // Free port 8088 for the server-mode stack.
  exec("docker", ["compose", ...SUBPATH, "stop", "proxytest"]);
}
if (run("--root") || run("--subpath")) {
  // Back to your stack as .env has it: the root, your own model, no fake.
  must("docker", ["compose", "up", "-d", "--wait", "--remove-orphans"]);
}
if (run("--server")) {
  const secret = (bytes) => randomBytes(bytes).toString("base64url");
  const admin = { email: "admin@smoke.test", initial: `Initial-${secret(12)}`, chosen: `Chosen-${secret(12)}` };
  writeFileSync(
    SERVER_ENV,
    [
      "# Written by scripts/smoke.mjs for one run, then deleted. Test values only.",
      "POSTGRES_USER=opsis",
      `POSTGRES_PASSWORD=${secret(24)}`,
      // A Fernet key: 32 random bytes, URL-safe base64 with padding.
      `ENCRYPTION_KEY=${randomBytes(32).toString("base64").replace(/\+/g, "-").replace(/\//g, "_")}`,
      `ADMIN_EMAIL=${admin.email}`,
      `ADMIN_INITIAL_PASSWORD=${admin.initial}`,
      `GOOGLE_CLIENT_SECRET=${secret(24)}`,
      // For the fake model only: it accepts any key.
      `ANTHROPIC_API_KEY=sk-fake-${secret(16)}`,
      "BIND_ADDRESS=127.0.0.1",
      "HTTP_PORT=8081",
      "",
    ].join("\n"),
  );
  try {
    // Always start from nothing, even after an interrupted run.
    exec("docker", ["compose", ...SERVER, "down", "-v", "--remove-orphans"]);
    exec("docker", ["compose", "stop", "proxytest"]);
    must("docker", ["compose", ...SERVER, "up", "-d", ...(built ? [] : build), "--wait"]);
    const status = playwright("http://localhost:8088/opsis/", {
      OPSIS_MODE: "server",
      OPSIS_ADMIN_EMAIL: admin.email,
      OPSIS_ADMIN_PASSWORD: admin.initial,
      OPSIS_ADMIN_NEW_PASSWORD: admin.chosen,
    });
    if (status !== 0) failed.push("server mode");
  } finally {
    exec("docker", ["compose", ...SERVER, "down", "-v", "--remove-orphans"]);
    rmSync(SERVER_ENV, { force: true });
    rmSync(join(root, "e2e", ".auth"), { recursive: true, force: true });
  }
}

console.log(failed.length === 0 ? "\nSmoke tests passed." : `\nSmoke tests FAILED: ${failed.join(", ")}.`);
process.exit(failed.length === 0 ? 0 : 1);
