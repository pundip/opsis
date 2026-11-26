// Browser smoke tests in three places, each in a test stack of its own
// (scripts/test-stack.mjs). Your own stack (Compose project "opsis", port 8080) is
// never touched and your .env is never read; a test stack that could reach either
// is refused before anything starts.
//   1. at the root, http://localhost:18080/ (local mode, AUTH_MODE=none)
//   2. behind the proxytest nginx at a sub-path, http://localhost:18088/opsis/
//   3. server mode at a sub-path, http://localhost:18089/opsis/: sign-in (local
//      accounts and a fake Google), share links, access tokens for MCP and the REST
//      API, email, the worker, and the assistant on Claude's API
// 1 and 2 run in the project opsis-test, 3 in opsis-test-server. Each starts from
// nothing, with fresh random secrets, and is removed afterwards, data and all.
// OPSIS_TEST_PORT_OFFSET moves the ports, OPSIS_TEST_PROJECT renames the projects.
//
//   npm run smoke                  all three, building the test image first
//   npm run smoke -- --no-build    all three, with the test image as it is
//   npm run smoke -- --root        only 1 (--subpath only 2, --server only 3)
//   npm run smoke -- --dry-run     check the test stacks' settings, start nothing
//
// Other arguments go to Playwright, for example -g "assistant".
// The assistant answers from a scripted fake model (e2e/fake-llm.mjs): Ollama's
// API at the root and the sub-path (compose.fakellm.yml), Claude's in server mode.
import { spawnSync } from "node:child_process";
import { existsSync, rmSync } from "node:fs";
import { join } from "node:path";
import { GuardError, ROOT, TestStack, guard, refuse, secret } from "./test-stack.mjs";

const args = process.argv.slice(2);
const FLAGS = ["--root", "--subpath", "--server", "--no-build", "--dry-run"];
const stages = ["--root", "--subpath", "--server"].filter((flag) => args.includes(flag));
const run = (stage) => stages.length === 0 || stages.includes(stage);
const dryRun = args.includes("--dry-run");
let build = !args.includes("--no-build");
const extra = args.filter((arg) => !FLAGS.includes(arg));

const AT_ROOT = { files: ["compose.yml", "compose.fakellm.yml"] };
const SUBPATH = { files: [...AT_ROOT.files, "compose.subpath.yml"], profiles: ["proxytest"] };
const SERVER = { files: ["compose.yml", "compose.servertest.yml"], profiles: ["server", "proxytest"] };

const admin = { email: "admin@smoke.test", initial: `Initial-${secret(12)}`, chosen: `Chosen-${secret(12)}` };
// The workspace's Claude key, for the fake model only (it accepts any key). The tests
// get its last four characters, to see which key answered.
const workspaceKey = `sk-fake-${secret(16)}`;

// Check everything before anything starts: the plans, then what Compose makes of them.
const stacks = [];
let local;
let server;
try {
  if (run("--root") || run("--subpath")) stacks.push((local = new TestStack("local", { dryRun })));
  if (run("--server")) {
    const values = {
      ADMIN_EMAIL: admin.email,
      ADMIN_INITIAL_PASSWORD: admin.initial,
      GOOGLE_CLIENT_SECRET: secret(24),
      ANTHROPIC_API_KEY: workspaceKey,
    };
    stacks.push((server = new TestStack("server", { values, dryRun })));
  }
  for (const stack of stacks) stack.write();
  if (local && run("--root")) local.check(AT_ROOT);
  if (local && run("--subpath")) local.check(SUBPATH);
  if (server) server.check(SERVER);
} catch (error) {
  for (const stack of stacks) stack.forget();
  refuse(error);
}

const targets = [
  local && run("--root") && { stage: "root", stack: local, url: local.url("HTTP_PORT") },
  local && run("--subpath") && { stage: "sub-path", stack: local, url: local.url("PROXYTEST_PORT", "/opsis/") },
  server && { stage: "server mode", stack: server, url: server.url("PROXYTEST_PORT", "/opsis/") },
].filter(Boolean);
for (const target of targets) {
  const problems = guard({ url: target.url });
  if (problems.length > 0) {
    for (const stack of stacks) stack.forget();
    refuse(new GuardError(problems));
  }
}

if (dryRun) {
  for (const stack of stacks) stack.forget();
  console.log("Dry run: the guard passed, and nothing was started. These test stacks would run:");
  for (const { stage, stack, url } of targets) console.log(`  ${stage.padEnd(12)} project ${stack.project.padEnd(20)} ${url}`);
  process.exit(0);
}

function exec(command, commandArgs, options = {}) {
  console.log(`\n> ${command} ${commandArgs.join(" ")}`);
  const result = spawnSync(command, commandArgs, { cwd: ROOT, stdio: "inherit", shell: process.platform === "win32", ...options });
  return result.status ?? 1;
}

function playwright(url, env = {}) {
  return exec("npx", ["playwright", "test", ...extra], {
    cwd: join(ROOT, "e2e"),
    env: { ...process.env, OPSIS_URL: url, OPSIS_FAKE_MODEL: "1", OPSIS_MODE: "local", ...env },
  });
}

/** On a failure, the api's last lines, before the stack is removed. */
function showLogs(stack, config) {
  stack.compose(config, ["logs", "--no-color", "--tail", "60", "api"]);
}

/** Bring a stack up in this configuration (building the test image once per run). */
function up(stack, config) {
  if (stack.compose(config, ["up", "-d", ...(build ? ["--build"] : []), "--wait"]) !== 0) {
    showLogs(stack, config);
    throw new Error(`Failed: docker compose up (project ${stack.project})`);
  }
  build = false;
}

if (!existsSync(join(ROOT, "e2e", "node_modules"))) {
  if (exec("npm", ["install"], { cwd: join(ROOT, "e2e") }) !== 0) process.exit(1);
  if (exec("npx", ["playwright", "install", "chromium"], { cwd: join(ROOT, "e2e") }) !== 0) process.exit(1);
}

const failed = [];
try {
  if (local) {
    try {
      local.compose(SUBPATH, ["down", "-v", "--remove-orphans"]); // from nothing, even after an interrupted run
      const google = { OPSIS_FAKE_GOOGLE_URL: local.url("FAKE_GOOGLE_PORT", "") };
      if (run("--root")) {
        up(local, AT_ROOT);
        if (playwright(local.url("HTTP_PORT"), google) !== 0) {
          failed.push("root");
          showLogs(local, AT_ROOT);
        }
      }
      if (run("--subpath")) {
        up(local, SUBPATH);
        if (playwright(local.url("PROXYTEST_PORT", "/opsis/"), google) !== 0) {
          failed.push("sub-path");
          showLogs(local, SUBPATH);
        }
      }
    } finally {
      local.remove(SUBPATH);
    }
  }
  if (server) {
    try {
      server.compose(SERVER, ["down", "-v", "--remove-orphans"]);
      up(server, SERVER);
      const status = playwright(server.url("PROXYTEST_PORT", "/opsis/"), {
        OPSIS_MODE: "server",
        OPSIS_ADMIN_EMAIL: admin.email,
        OPSIS_ADMIN_PASSWORD: admin.initial,
        OPSIS_ADMIN_NEW_PASSWORD: admin.chosen,
        OPSIS_WORKSPACE_KEY_END: workspaceKey.slice(-4),
        OPSIS_FAKE_GOOGLE_URL: server.url("FAKE_GOOGLE_PORT", ""),
        OPSIS_FAKE_MAIL_URL: server.url("FAKE_SMTP_PORT", ""),
      });
      if (status !== 0) {
        failed.push("server mode");
        showLogs(server, SERVER);
      }
    } finally {
      server.remove(SERVER);
      rmSync(join(ROOT, "e2e", ".auth"), { recursive: true, force: true });
    }
  }
} catch (error) {
  console.error(`\n${error instanceof Error ? error.message : String(error)}`);
  failed.push("setup");
}

console.log(failed.length === 0 ? "\nSmoke tests passed." : `\nSmoke tests FAILED: ${failed.join(", ")}.`);
process.exit(failed.length === 0 ? 0 : 1);
