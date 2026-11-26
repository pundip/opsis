// Test stacks, kept apart from your own. Every stack that the smoke tests, the
// backend's tests or the benchmark start runs under a Compose project of its own,
// with an env file written here (fresh random secrets: your .env is never read) and
// ports of its own, and is removed afterwards, data and all. Your own stack is the
// project "opsis" on port 8080: a test stack that would be either is refused before
// anything starts, and so is one that Compose, reading its files, would set up so it
// could reach yours. scripts/test-stack.test.mjs proves the guard (npm run test:scripts).
//
//   OPSIS_TEST_PROJECT      the test projects' name: default opsis-test, with -server
//                           for server mode, -backend for the backend's tests and
//                           -bench for the benchmark
//   OPSIS_TEST_PORT_OFFSET  added to each usual port: default 10000, so the root is on
//                           18080, the sub-path on 18088 and server mode on 18089
//
// In a second checkout (a worktree), set both, so that its tests keep off this one's.
import { spawnSync } from "node:child_process";
import { randomBytes } from "node:crypto";
import { readdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

export const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
/** Your own stack, which no test may touch: compose.yml's project name, and its port. */
export const YOURS = { project: "opsis", port: 8080 };
export const DEFAULTS = { project: "opsis-test", offset: 10000 };
const MAX_OFFSET = 50000;

/** Each kind of test stack: its project's suffix, the template its env file starts
 * from, and its usual ports (before the offset). */
export const KINDS = {
  local: {
    suffix: "",
    template: ".env.local.example",
    ports: { HTTP_PORT: 8080, PROXYTEST_PORT: 8088, FAKE_GOOGLE_PORT: 9700 },
  },
  server: {
    suffix: "-server",
    template: ".env.server.example",
    ports: { HTTP_PORT: 8081, PROXYTEST_PORT: 8089, FAKE_GOOGLE_PORT: 9701, FAKE_SMTP_PORT: 9600, FAKE_OIDC_PORT: 9400 },
  },
  backend: {
    suffix: "-backend",
    template: ".env.local.example",
    ports: { HTTP_PORT: 8082, PROXYTEST_PORT: 8090, WEB_PORT: 5173 },
  },
  bench: {
    suffix: "-bench",
    template: ".env.local.example",
    ports: { HTTP_PORT: 8083, PROXYTEST_PORT: 8091 },
  },
};
/** The services that read the env file (compose.yml's x-app). */
const APP_SERVICES = ["config-check", "api", "worker"];

export class GuardError extends Error {
  constructor(problems) {
    super(`Refusing to start a test stack:\n${problems.map((problem) => `  - ${problem}`).join("\n")}`);
    this.problems = problems;
  }
}

export const secret = (bytes) => randomBytes(bytes).toString("base64url");
/** A Fernet key: 32 random bytes, URL-safe base64 with padding. */
const fernetKey = () => randomBytes(32).toString("base64").replace(/\+/g, "-").replace(/\//g, "_");

function portOf(url) {
  try {
    const parsed = new URL(url);
    return Number(parsed.port || (parsed.protocol === "https:" ? 443 : 80));
  } catch {
    return null;
  }
}

const sameFile = (a, b) =>
  process.platform === "win32" ? resolve(ROOT, a).toLowerCase() === resolve(ROOT, b).toLowerCase() : resolve(ROOT, a) === resolve(ROOT, b);

/** Why a test stack could reach your own: a list of problems, empty when there are none.
 * Give what is known: the project name (and the prefix it was made from), the port
 * offset, the ports by name, the address tests will go to, the env file. */
export function guard({ prefix, project, offset, ports = {}, url, envFile } = {}) {
  const problems = [];
  for (const name of new Set([prefix, project].filter((value) => value !== undefined))) {
    if (name === YOURS.project) {
      problems.push(
        `The Compose project would be "${name}", your own stack's. Test stacks run under a project of their own: ` +
          `set OPSIS_TEST_PROJECT to another name, or leave it unset for ${DEFAULTS.project}.`,
      );
    } else if (!/^[a-z0-9][a-z0-9_-]*$/.test(name)) {
      problems.push(`"${name}" is not a Compose project name: use lowercase letters, digits, dashes and underscores.`);
    }
  }
  if (offset !== undefined) {
    const value = Number(offset);
    if (String(offset).trim() === "" || !Number.isInteger(value) || value < 1 || value > MAX_OFFSET) {
      problems.push(
        `OPSIS_TEST_PORT_OFFSET is ${JSON.stringify(String(offset))}: it must be a whole number from 1 to ${MAX_OFFSET}, ` +
          `so that test stacks keep off your own stack's ports. Leave it unset for ${DEFAULTS.offset} (the root on ${YOURS.port + DEFAULTS.offset}).`,
      );
    }
  }
  for (const [name, port] of Object.entries(ports)) {
    if (Number(port) === YOURS.port) {
      problems.push(`${name} would be ${YOURS.port}, your own stack's port. Test stacks use ports of their own (see OPSIS_TEST_PORT_OFFSET).`);
    }
  }
  if (url !== undefined && portOf(url) === YOURS.port) {
    problems.push(`The tests would go to ${url}, on your own stack's port ${YOURS.port}.`);
  }
  if (envFile !== undefined && sameFile(envFile, ".env")) {
    problems.push("The env file would be .env, your own. Test stacks write one of their own.");
  }
  return problems;
}

/** What a test stack of this kind would be, from OPSIS_TEST_PROJECT and
 * OPSIS_TEST_PORT_OFFSET. Throws GuardError when it could reach your own stack. */
export function plan(kind, env = process.env) {
  const { suffix, ports: usual } = KINDS[kind];
  const prefix = env.OPSIS_TEST_PROJECT || DEFAULTS.project;
  const offset = env.OPSIS_TEST_PORT_OFFSET ?? String(DEFAULTS.offset);
  const project = `${prefix}${suffix}`;
  const ports = Object.fromEntries(Object.entries(usual).map(([name, port]) => [name, port + Number(offset)]));
  const envFile = `.env.${project}`;
  const problems = guard({ prefix, project, offset, ports, envFile });
  if (problems.length > 0) throw new GuardError(problems);
  return { kind, prefix, project, offset: Number(offset), ports, envFile };
}

/** Why Compose's own view of a test stack (docker compose config --format json) could
 * reach your stack: a list of problems, empty when there are none. */
export function checkConfig(config, stack) {
  const problems = [];
  if (config.name === YOURS.project) problems.push(`Compose would run this as the project "${config.name}", your own stack's.`);
  else if (config.name !== stack.project) problems.push(`Compose would run this as the project "${config.name}", not "${stack.project}".`);
  const allowed = new Set(Object.values(stack.ports).map(Number));
  for (const [name, service] of Object.entries(config.services ?? {})) {
    for (const port of service.ports ?? []) {
      if (port.published === undefined || port.published === "") continue; // not published to this computer
      const published = Number(port.published);
      if (published === YOURS.port) problems.push(`The ${name} service would publish port ${YOURS.port}, your own stack's.`);
      else if (!allowed.has(published)) {
        problems.push(`The ${name} service would publish port ${port.published}, which is not one of this test stack's (${[...allowed].join(", ")}).`);
      }
      if (port.host_ip !== "127.0.0.1") problems.push(`The ${name} service would listen on ${port.host_ip || "every address"}, not only on 127.0.0.1.`);
    }
    const image = String(service.image ?? "");
    if (image.startsWith("opsis/") && !image.endsWith(`:${stack.image}`)) {
      problems.push(`The ${name} service would use the image ${image}, not one tagged ${stack.image} for the test stacks.`);
    }
    if (APP_SERVICES.includes(name)) {
      const environment = service.environment ?? {};
      if (environment.OPSIS_TEST_STACK !== stack.token) {
        problems.push(`The ${name} service would not read the test env file ${stack.envFile}, but another (perhaps your .env).`);
      }
      const base = environment.PUBLIC_BASE_URL;
      if (base && portOf(base) === YOURS.port) problems.push(`The ${name} service would have PUBLIC_BASE_URL=${base}, your own stack's address.`);
    }
  }
  return problems;
}

function readTemplate(name) {
  const values = {};
  for (const line of readFileSync(join(ROOT, name), "utf8").split(/\r?\n/)) {
    const match = /^([A-Z][A-Z0-9_]*)=(.*)$/.exec(line.trim());
    if (match) values[match[1]] = match[2];
  }
  return values;
}

/** Every variable the Compose files read (${NAME...}). */
function composeVariables() {
  const names = new Set();
  for (const file of readdirSync(ROOT).filter((name) => /^compose.*\.ya?ml$/.test(name))) {
    for (const match of readFileSync(join(ROOT, file), "utf8").matchAll(/\$\{([A-Za-z_][A-Za-z0-9_]*)/g)) names.add(match[1]);
  }
  return names;
}

/** Your environment for docker, less anything that could change a test stack's
 * settings: COMPOSE_* and every variable the Compose files or the env file set. Those
 * come from the test env file, or from the Compose files' own defaults. */
function dockerEnv(values, env) {
  const drop = new Set([...Object.keys(values), ...composeVariables()].map((name) => name.toUpperCase()));
  return Object.fromEntries(
    Object.entries(env).filter(([name]) => {
      const upper = name.toUpperCase();
      return !upper.startsWith("COMPOSE_") && !drop.has(upper);
    }),
  );
}

/** One test stack. Making one checks the plan (GuardError if it could reach your own);
 * nothing is written or started until write() and compose(). A dry run's env file has
 * a name of its own, so checking leaves a real run of the same stack alone. */
export class TestStack {
  constructor(kind, { env = process.env, values = {}, dryRun = false } = {}) {
    Object.assign(this, plan(kind, env));
    if (dryRun) this.envFile = `${this.envFile}.dry-run-${process.pid}`;
    this.token = randomBytes(12).toString("hex");
    this.image = this.prefix;
    this.envPath = join(ROOT, this.envFile);
    this.values = {
      ...readTemplate(KINDS[kind].template),
      // Profiles come from --profile only: a template's (server's caddy, say) never start.
      COMPOSE_PROFILES: "",
      OPSIS_TEST_STACK: this.token,
      OPSIS_ENV_FILE: this.envFile,
      OPSIS_VERSION: this.image,
      OPSIS_DEV_VERSION: this.image,
      BIND_ADDRESS: "127.0.0.1",
      ...Object.fromEntries(Object.entries(this.ports).map(([name, port]) => [name, String(port)])),
      PUBLIC_BASE_URL:
        kind === "server" ? `http://localhost:${this.ports.PROXYTEST_PORT}/opsis` : `http://localhost:${this.ports.HTTP_PORT}`,
      POSTGRES_USER: "opsis",
      POSTGRES_PASSWORD: secret(24),
      POSTGRES_DB: "opsis",
      ENCRYPTION_KEY: fernetKey(),
      ...values,
    };
    this.env = dockerEnv(this.values, env);
  }

  /** The address of something this stack publishes, by its port's name. */
  url(portName, path = "/") {
    return `http://localhost:${this.ports[portName]}${path}`;
  }

  /** docker's arguments for a Compose command on this stack. */
  args({ files, profiles = [] }, command) {
    return [
      "compose",
      "-p",
      this.project,
      "--env-file",
      this.envFile,
      ...files.flatMap((file) => ["-f", file]),
      ...profiles.flatMap((profile) => ["--profile", profile]),
      ...command,
    ];
  }

  write() {
    const lines = Object.entries(this.values).map(([name, value]) => `${name}=${value}`);
    const header = [
      `# Written by scripts/test-stack.mjs for the test stack ${this.project}, and deleted`,
      "# with it. Test values only: your own .env is never read.",
    ];
    writeFileSync(this.envPath, [...header, ...lines, ""].join("\n"));
  }

  /** Ask Compose how it would set this stack up, and refuse what could reach yours.
   * Starts nothing: `config` only reads the files. */
  check(config) {
    const result = spawnSync("docker", this.args(config, ["config", "--format", "json"]), {
      cwd: ROOT,
      env: this.env,
      encoding: "utf8",
      maxBuffer: 64 * 1024 * 1024,
    });
    if (result.status !== 0) {
      throw new Error(`docker compose config failed for ${this.project}:\n${result.error?.message ?? result.stderr}`);
    }
    const problems = checkConfig(JSON.parse(result.stdout), this);
    if (problems.length > 0) throw new GuardError(problems);
  }

  /** Run a Compose command on this stack, showing it and its output. Returns its status. */
  compose(config, command, options = {}) {
    const args = this.args(config, command);
    console.log(`\n> docker ${args.join(" ")}`);
    const result = spawnSync("docker", args, { cwd: ROOT, env: this.env, stdio: "inherit", ...options });
    return result.status ?? 1;
  }

  /** As compose(), but a failure throws. */
  must(config, command) {
    if (this.compose(config, command) !== 0) throw new Error(`Failed: docker compose ${command.join(" ")} (project ${this.project})`);
  }

  /** Remove the stack, data and all, and its env file. */
  remove(config) {
    this.compose(config, ["down", "-v", "--remove-orphans"]);
    this.forget();
  }

  forget() {
    rmSync(this.envPath, { force: true });
  }
}

/** Say why a script stopped before starting anything, and exit: 2 when the guard
 * refused, 1 for anything else. */
export function refuse(error) {
  if (error instanceof GuardError) {
    console.error(
      `\n${error.message}\nNothing was started. Your own stack (Compose project "${YOURS.project}", port ${YOURS.port}) ` +
        "and your .env were not touched.",
    );
    process.exit(2);
  }
  console.error(`\n${error instanceof Error ? error.message : String(error)}`);
  process.exit(1);
}
