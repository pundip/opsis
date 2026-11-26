// The backend's tests (pytest), or its lint and type checks, in a test stack of
// their own (scripts/test-stack.mjs): the project opsis-test-backend, with an env file
// written for it and its own Postgres and sample MySQL, removed afterwards, data and
// all. Your own stack (Compose project "opsis", port 8080) is never touched and your
// .env is never read.
//
//   npm run test:backend                      every test
//   npm run test:backend -- -k byo -x         pytest's own arguments
//   npm run lint:backend                      ruff and mypy (no database)
//   node scripts/test-backend.mjs --dry-run   check the test stack's settings, start nothing
import { TestStack, refuse } from "./test-stack.mjs";

const args = process.argv.slice(2);
const lint = args.includes("--lint");
const dryRun = args.includes("--dry-run");
const rest = args.filter((arg) => !["--lint", "--dry-run"].includes(arg));
const TOOLS = { files: ["compose.yml", "compose.dev.yml"], profiles: ["tools"] };
const LINT =
  "ruff check --no-cache opsis tests && ruff format --no-cache --check opsis tests && mypy --cache-dir=/tmp/mypy";

let stack;
try {
  stack = new TestStack("backend", { dryRun });
  stack.write();
  stack.check(TOOLS);
} catch (error) {
  stack?.forget();
  refuse(error);
}

if (dryRun) {
  stack.forget();
  console.log(`Dry run: the guard passed, and nothing was started. The tests would run in the project ${stack.project}.`);
  process.exit(0);
}

let status = 1;
try {
  if (lint) {
    status = stack.compose(TOOLS, ["run", "--rm", "--build", "--no-deps", "tools", "sh", "-c", LINT]);
  } else {
    stack.compose(TOOLS, ["down", "-v", "--remove-orphans"]); // from nothing, even after an interrupted run
    status = stack.compose(TOOLS, ["run", "--rm", "--build", "tools", "pytest", ...rest]);
  }
} finally {
  stack.remove(TOOLS);
}
process.exit(status);
