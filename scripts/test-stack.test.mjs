// The guard that keeps tests off your own stack (scripts/test-stack.mjs): what it
// refuses, and that the smoke tests, the backend's tests, the benchmark and
// Playwright refuse to start when pointed at port 8080 or the project "opsis".
//
//   npm run test:scripts
//
// The scripts are run with docker pointed at a daemon that does not exist, and with
// --dry-run where they have it, so that even a broken guard could start nothing.
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { existsSync, readFileSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { describe, test } from "node:test";
import { DEFAULTS, GuardError, KINDS, ROOT, YOURS, checkConfig, guard, plan } from "./test-stack.mjs";

// Its own project prefix, so that a dry run never rewrites a running test stack's env file.
const PREFIX = `opsis-guardtest-${process.pid}`;
const NOWHERE = "tcp://127.0.0.1:1";

function script(file, args, env) {
  const base = { ...process.env };
  for (const name of Object.keys(base)) {
    if (/^(OPSIS_|COMPOSE_|DOCKER_CONTEXT$)/i.test(name)) delete base[name];
  }
  const result = spawnSync(process.execPath, [join(ROOT, "scripts", file), ...args], {
    cwd: ROOT,
    env: { ...base, DOCKER_HOST: NOWHERE, OPSIS_TEST_PROJECT: PREFIX, ...env },
    encoding: "utf8",
    timeout: 120_000,
  });
  return { status: result.status, output: `${result.stdout}${result.stderr}` };
}

// The env files a refused stack, or a dry run, could have left: none may be there after.
const OURS = new Set(["", "-server", "-backend", "-bench"].flatMap((suffix) => [`.env.opsis${suffix}`, `.env.${PREFIX}${suffix}`]));
const leftovers = () => readdirSync(ROOT).filter((name) => OURS.has(name) || name.startsWith(`.env.${PREFIX}`));
const hasDocker = spawnSync("docker", ["compose", "version"], { encoding: "utf8" }).status === 0;
const hasPlaywright = existsSync(join(ROOT, "e2e", "node_modules", "@playwright", "test"));

describe("guard", () => {
  test("refuses your own project, as the name or as the prefix", () => {
    for (const problems of [guard({ project: "opsis" }), guard({ prefix: "opsis", project: "opsis-server" })]) {
      assert.equal(problems.length, 1);
      assert.match(problems[0], /"opsis", your own stack's/);
      assert.match(problems[0], /OPSIS_TEST_PROJECT/);
    }
  });

  test("refuses port 8080, wherever it comes from", () => {
    assert.match(guard({ ports: { HTTP_PORT: 8080 } })[0], /HTTP_PORT would be 8080, your own stack's port/);
    assert.match(guard({ ports: { PROXYTEST_PORT: "8080" } })[0], /8080/);
    for (const url of ["http://localhost:8080/", "http://127.0.0.1:8080/opsis/"]) {
      assert.match(guard({ url })[0], /your own stack's port 8080/);
    }
  });

  test("refuses an offset that would put a test stack on your ports", () => {
    for (const offset of ["0", "", " ", "-10", "1.5", "abc", "60000"]) {
      const problems = guard({ offset });
      assert.equal(problems.length, 1, `offset ${JSON.stringify(offset)}`);
      assert.match(problems[0], /OPSIS_TEST_PORT_OFFSET/);
    }
  });

  test("refuses your .env as the env file", () => {
    for (const envFile of [".env", "./.env", join(ROOT, ".env")]) {
      assert.match(guard({ envFile })[0], /\.env, your own/);
    }
  });

  test("passes a test stack's plan", () => {
    assert.deepEqual(
      guard({ prefix: "opsis-test", project: "opsis-test", offset: "10000", ports: { HTTP_PORT: 18080 }, url: "http://localhost:18080/", envFile: ".env.opsis-test" }),
      [],
    );
  });
});

describe("plan", () => {
  test("by default: its own project, env file and ports", () => {
    const local = plan("local", {});
    assert.equal(local.project, DEFAULTS.project);
    assert.equal(local.envFile, `.env.${DEFAULTS.project}`);
    assert.deepEqual(local.ports, { HTTP_PORT: 18080, PROXYTEST_PORT: 18088, FAKE_GOOGLE_PORT: 19700 });
    assert.equal(plan("server", {}).project, `${DEFAULTS.project}-server`);
    assert.equal(plan("backend", {}).project, `${DEFAULTS.project}-backend`);
  });

  test("no two kinds share a port, and none is yours", () => {
    const ports = Object.keys(KINDS).flatMap((kind) => Object.values(plan(kind, {}).ports));
    assert.equal(new Set(ports).size, ports.length);
    assert.ok(!ports.includes(YOURS.port));
  });

  test("throws for the project opsis, and for an offset of 0 (port 8080)", () => {
    for (const kind of Object.keys(KINDS)) {
      assert.throws(() => plan(kind, { OPSIS_TEST_PROJECT: "opsis" }), GuardError);
      assert.throws(() => plan(kind, { OPSIS_TEST_PORT_OFFSET: "0" }), GuardError);
    }
    const error = (() => {
      try {
        plan("local", { OPSIS_TEST_PORT_OFFSET: "0" });
      } catch (caught) {
        return caught;
      }
    })();
    assert.ok(error.problems.some((problem) => /HTTP_PORT would be 8080/.test(problem)));
  });
});

describe("checkConfig", () => {
  const stack = { project: "opsis-test", ports: { HTTP_PORT: 18080, PROXYTEST_PORT: 18088 }, image: "opsis-test", token: "t0k3n", envFile: ".env.opsis-test" };
  const app = (extra = {}) => ({
    image: "opsis/app:opsis-test",
    environment: { OPSIS_TEST_STACK: "t0k3n", PUBLIC_BASE_URL: "http://localhost:18080" },
    ...extra,
  });
  const good = () => ({
    name: "opsis-test",
    services: {
      "config-check": app(),
      api: app({ ports: [{ host_ip: "127.0.0.1", target: 8080, published: "18080" }] }),
      postgres: { image: "postgres:16-bookworm" },
      proxytest: { image: "nginx:stable-alpine", ports: [{ host_ip: "127.0.0.1", target: 80, published: "18088" }] },
    },
  });

  test("passes a test stack as Compose sets it up", () => {
    assert.deepEqual(checkConfig(good(), stack), []);
  });

  test("refuses what could reach your own stack", () => {
    const cases = [
      [(c) => (c.name = "opsis"), /project "opsis", your own stack's/],
      [(c) => (c.services.api.ports[0].published = "8080"), /publish port 8080, your own stack's/],
      [(c) => (c.services.caddy = { image: "caddy:2-alpine", ports: [{ host_ip: "127.0.0.1", target: 80, published: "80" }] }), /port 80, which is not one of this test stack's/],
      [(c) => (c.services.api.ports[0].host_ip = "0.0.0.0"), /listen on 0\.0\.0\.0/],
      [(c) => delete c.services.api.environment.OPSIS_TEST_STACK, /would not read the test env file/],
      [(c) => (c.services["config-check"].environment.PUBLIC_BASE_URL = "http://localhost:8080"), /PUBLIC_BASE_URL=http:\/\/localhost:8080, your own stack's address/],
      [(c) => (c.services.api.image = "opsis/app:dev"), /image opsis\/app:dev/],
    ];
    for (const [change, expected] of cases) {
      const config = good();
      change(config);
      const problems = checkConfig(config, stack);
      assert.equal(problems.length, 1, `${expected}: ${problems.join(" | ")}`);
      assert.match(problems[0], expected);
    }
  });
});

describe("the scripts refuse to start", () => {
  const refused = (result) => {
    assert.equal(result.status, 2, result.output);
    assert.match(result.output, /Refusing to start a test stack/);
    assert.match(result.output, /Nothing was started/);
    assert.deepEqual(leftovers(), [], "no env file is left behind");
  };

  test("smoke, aimed at port 8080 or the project opsis", () => {
    refused(script("smoke.mjs", ["--dry-run"], { OPSIS_TEST_PORT_OFFSET: "0" }));
    refused(script("smoke.mjs", ["--dry-run", "--root"], { OPSIS_TEST_PROJECT: "opsis" }));
    const output = script("smoke.mjs", ["--dry-run", "--server"], { OPSIS_TEST_PROJECT: "opsis" }).output;
    assert.match(output, /"opsis", your own stack's/);
  });

  test("the backend's tests and lint, and the benchmark", () => {
    refused(script("test-backend.mjs", ["--dry-run"], { OPSIS_TEST_PROJECT: "opsis" }));
    refused(script("test-backend.mjs", ["--lint"], { OPSIS_TEST_PORT_OFFSET: "0" }));
    // -3 would put the benchmark's api (usually 8083) on 8080.
    const bench = script("benchmark.mjs", ["--rows", "10"], { OPSIS_TEST_PORT_OFFSET: "-3" });
    refused(bench);
    assert.match(bench.output, /HTTP_PORT would be 8080/);
    refused(script("benchmark.mjs", ["--rows", "10"], { OPSIS_TEST_PROJECT: "opsis" }));
  });

  test("Playwright, aimed at port 8080", { skip: !hasPlaywright && "e2e/node_modules is not installed" }, () => {
    const result = spawnSync("npx playwright test --list", {
      cwd: join(ROOT, "e2e"),
      env: { ...process.env, OPSIS_URL: "http://localhost:8080/" },
      encoding: "utf8",
      shell: true,
      timeout: 120_000,
    });
    assert.notEqual(result.status, 0);
    assert.match(`${result.stdout}${result.stderr}`, /Refusing to run the smoke tests against http:\/\/localhost:8080\//);
  });
});

describe("a safe dry run", { skip: !hasDocker && "docker is not installed" }, () => {
  test("smoke: Compose's own view of every stage passes, and nothing starts", () => {
    const result = script("smoke.mjs", ["--dry-run"], {});
    assert.equal(result.status, 0, result.output);
    assert.match(result.output, new RegExp(`root +project ${PREFIX} +http://localhost:18080/`));
    assert.match(result.output, new RegExp(`sub-path +project ${PREFIX} +http://localhost:18088/opsis/`));
    assert.match(result.output, new RegExp(`server mode +project ${PREFIX}-server +http://localhost:18089/opsis/`));
    assert.deepEqual(leftovers(), []);
  });

  test("the backend's tests", () => {
    const result = script("test-backend.mjs", ["--dry-run"], {});
    assert.equal(result.status, 0, result.output);
    assert.match(result.output, new RegExp(`project ${PREFIX}-backend`));
    assert.deepEqual(leftovers(), []);
  });

  test("a dry run leaves the env files of a run in progress alone", () => {
    const running = [`.env.${PREFIX}`, `.env.${PREFIX}-server`, `.env.${PREFIX}-backend`].map((name) => join(ROOT, name));
    try {
      for (const file of running) writeFileSync(file, "# a run in progress\n");
      assert.equal(script("smoke.mjs", ["--dry-run"], {}).status, 0);
      assert.equal(script("test-backend.mjs", ["--dry-run"], {}).status, 0);
      for (const file of running) assert.equal(readFileSync(file, "utf8"), "# a run in progress\n");
    } finally {
      for (const file of running) rmSync(file, { force: true });
    }
    assert.deepEqual(leftovers(), []);
  });
});
