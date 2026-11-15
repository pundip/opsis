// Runs the browser smoke tests twice: at the root (http://localhost:8080) and
// behind the proxytest nginx at a sub-path (http://localhost:8088/opsis/).
//
//   npm run smoke                 both, rebuilding the image first
//   npm run smoke -- --no-build   both, with the image as it is
//   npm run smoke -- --root       only the root
//   npm run smoke -- --subpath    only the sub-path
//
// It expects .env to be in plain local mode (PUBLIC_BASE_URL=http://localhost:8080).
// The stack is left running at the root when it finishes.
import { spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const args = process.argv.slice(2);
const only = args.includes("--root") ? "root" : args.includes("--subpath") ? "subpath" : "both";
const build = args.includes("--no-build") ? [] : ["--build"];
const extra = args.filter((arg) => !["--root", "--subpath", "--no-build"].includes(arg));

function run(command, commandArgs, options = {}) {
  console.log(`\n> ${command} ${commandArgs.join(" ")}`);
  const result = spawnSync(command, commandArgs, { cwd: root, stdio: "inherit", shell: process.platform === "win32", ...options });
  return result.status ?? 1;
}

function must(command, commandArgs, options) {
  const status = run(command, commandArgs, options);
  if (status !== 0) {
    console.error(`\nFailed: ${command} ${commandArgs.join(" ")}`);
    process.exit(status);
  }
}

const SUBPATH = ["-f", "compose.yml", "-f", "compose.subpath.yml", "--profile", "proxytest"];

function playwright(url) {
  return run("npx", ["playwright", "test", ...extra], { cwd: join(root, "e2e"), env: { ...process.env, OPSIS_URL: url } });
}

if (!existsSync(join(root, "e2e", "node_modules"))) {
  must("npm", ["install"], { cwd: join(root, "e2e") });
  must("npx", ["playwright", "install", "chromium"], { cwd: join(root, "e2e") });
}

let failed = 0;
if (only !== "subpath") {
  must("docker", ["compose", "up", "-d", ...build, "--wait"]);
  failed += playwright("http://localhost:8080/") === 0 ? 0 : 1;
}
if (only !== "root") {
  must("docker", ["compose", ...SUBPATH, "up", "-d", ...(only === "subpath" ? build : []), "--wait"]);
  failed += playwright("http://localhost:8088/opsis/") === 0 ? 0 : 1;
  // Back to the root, and stop the test proxy.
  run("docker", ["compose", ...SUBPATH, "stop", "proxytest"]);
  must("docker", ["compose", "up", "-d", "--wait"]);
}

console.log(failed === 0 ? "\nSmoke tests passed." : `\nSmoke tests FAILED in ${failed} run(s).`);
process.exit(failed === 0 ? 0 : 1);
