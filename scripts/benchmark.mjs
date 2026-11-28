// Query benchmark, in a test stack of its own (scripts/test-stack.mjs): the project
// opsis-test-bench at http://localhost:18083, never your own stack (Compose project
// "opsis", port 8080). Needs only Node and Docker on the host.
//
//   node scripts/benchmark.mjs [--rows 5000000] [--rule] [--no-build] [--keep]
//   node scripts/benchmark.mjs --remove     remove a stack that --keep left running
//
// What it does:
//   0. Starts the test stack from nothing, building the test image first.
//   1. Creates a synthetic `bench.gifts` table (and `bench.donors`) in its Postgres.
//   2. Connects Opsis to it, creates datasets and times the first refresh (ingestion).
//   3. Runs five typical dashboard queries cold (first run) and warm (cached).
//   3b. Opens a typical dashboard the way a browser does (every item's data, six
//       requests at a time): cold, warm, and after a filter change. The brief's
//       target is under 1.5 s warm on 10 million rows.
//   4. Fires 10 different queries at once, twice, and measures how long /healthz takes
//      to answer meanwhile (it must stay fast: queries run off the event loop).
//   4b. With --rule: puts a row-level security rule on bench_donors that applies to
//       admins too (tier <> 'lapsed'), so every gift is read through the relationship
//       to its donor (R2 of ADR-032), and runs the queries and the dashboard again.
//   5. Samples `docker stats` throughout and reports the api container's peak memory.
//   6. Removes the test stack, data and all, unless --keep is given.
import { execFileSync, spawn } from "node:child_process";
import { ROOT, TestStack, refuse } from "./test-stack.mjs";

const args = process.argv.slice(2);
const option = (name, fallback) => {
  const index = args.indexOf(`--${name}`);
  return index >= 0 ? args[index + 1] : fallback;
};
const ROWS = Number(option("rows", "5000000"));
const KEEP = args.includes("--keep");
const RULE = args.includes("--rule");
const STACK = { files: ["compose.yml"] };
let stack;
try {
  stack = new TestStack("bench");
  stack.write();
  stack.check(STACK);
} catch (error) {
  stack?.forget();
  refuse(error);
}
if (args.includes("--remove")) {
  stack.remove(STACK);
  process.exit(0);
}
const BASE = stack.url("HTTP_PORT", "");
const PG_USER = stack.values.POSTGRES_USER;
const PG_DB = stack.values.POSTGRES_DB;
const PG_PASSWORD = stack.values.POSTGRES_PASSWORD;

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const ms = (start) => Math.round(performance.now() - start);

function psql(sql) {
  return execFileSync(
    "docker",
    stack.args(STACK, ["exec", "-T", "postgres", "psql", "-U", PG_USER, "-d", PG_DB, "-v", "ON_ERROR_STOP=1", "-At", "-c", sql]),
    { cwd: ROOT, env: stack.env, encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] },
  ).trim();
}

// ---- memory sampling -----------------------------------------------------------------
const peak = {};
let sampling = true;
function toMb(text) {
  const match = /([\d.]+)\s*(KiB|MiB|GiB|B)/.exec(text);
  if (!match) return 0;
  const factor = { B: 1 / 1048576, KiB: 1 / 1024, MiB: 1, GiB: 1024 }[match[2]];
  return Number(match[1]) * factor;
}
async function sampleMemory() {
  while (sampling) {
    await new Promise((resolve) => {
      const child = spawn("docker", ["stats", "--no-stream", "--format", "{{.Name}}\t{{.MemUsage}}"]);
      let out = "";
      child.stdout.on("data", (chunk) => (out += chunk));
      child.on("close", () => {
        for (const line of out.split("\n")) {
          const [name, usage] = line.split("\t");
          if (!usage || !name.startsWith(`${stack.project}-`)) continue;
          const key = name.slice(stack.project.length + 1).replace(/-\d+$/, "");
          if (!["api", "worker", "postgres"].includes(key)) continue;
          peak[key] = Math.max(peak[key] ?? 0, toMb(usage));
        }
        resolve();
      });
      child.on("error", resolve);
    });
  }
}
function resetPeak() {
  for (const key of Object.keys(peak)) delete peak[key];
}

// ---- API client ------------------------------------------------------------------------
let cookie = "";
let csrf = "";
async function api(method, path, body) {
  const headers = { Accept: "application/json", Cookie: cookie };
  if (method !== "GET") headers["X-CSRF-Token"] = csrf;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const send = () => fetch(BASE + path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  // An idle keep-alive connection may have been closed by the server: retry once.
  const response = await send().catch(() => send());
  const setCookie = response.headers.get("set-cookie");
  if (setCookie?.startsWith("opsis_csrf=")) {
    csrf = setCookie.split(";")[0].split("=")[1];
    cookie = `opsis_csrf=${csrf}`;
  }
  const text = await response.text();
  if (!response.ok) throw new Error(`${method} ${path} -> ${response.status}: ${text.slice(0, 400)}`);
  return text ? JSON.parse(text) : null;
}
async function timedQuery(query) {
  const start = performance.now();
  const result = await api("POST", "/api/query", { query });
  return { wall: ms(start), engine: Math.round(result.elapsedMs), cacheHit: result.cacheHit, rows: result.rowCount };
}

// ---- queries ---------------------------------------------------------------------------
const QUERIES = {
  "KPI: total, count, donors, last 12 months": {
    dataset: "bench_gifts",
    measures: ["bench_gifts.total_amount", "bench_gifts.row_count", "bench_gifts.donors"],
    timeDimension: { field: "bench_gifts.gift_date", range: { relative: "last_12_months" } },
  },
  "Bar: total by fund, filtered to two regions": {
    dataset: "bench_gifts",
    dimensions: ["bench_gifts.fund"],
    measures: ["bench_gifts.total_amount", "bench_gifts.row_count"],
    filters: [{ field: "bench_gifts.region", op: "in", values: ["VIC", "NSW"] }],
    sort: [{ field: "bench_gifts.total_amount", dir: "desc" }],
  },
  "Line: total by month and channel, 3 years": {
    dataset: "bench_gifts",
    dimensions: ["bench_gifts.channel"],
    measures: ["bench_gifts.total_amount"],
    timeDimension: { field: "bench_gifts.gift_date", grain: "month" },
  },
  "Join: total by donor tier and financial quarter": {
    dataset: "bench_gifts",
    dimensions: ["bench_donors.tier"],
    measures: ["bench_gifts.total_amount", "bench_gifts.online_amount"],
    timeDimension: { field: "bench_gifts.gift_date", grain: "fiscal_quarter" },
  },
  "Table: top 100 donors by total (high cardinality)": {
    dataset: "bench_gifts",
    dimensions: ["bench_gifts.donor_id"],
    measures: ["bench_gifts.total_amount", "bench_gifts.row_count"],
    sort: [{ field: "bench_gifts.total_amount", dir: "desc" }],
    limit: 100,
  },
};

const GIFTS_MODEL = `
dimensions:
  fund: {column: fund, type: string}
  region: {column: region, type: string}
  channel: {column: channel, type: string}
  donor_id: {column: donor_id, type: number}
  gift_date: {column: gift_date, type: time}
  created_at: {column: created_at, type: time}
measures:
  row_count: {agg: count}
  total_amount: {agg: sum, column: amount}
  donors: {agg: count_distinct, column: donor_id}
  online_amount: {agg: sum, column: amount, filter: "channel = 'online'"}
joins:
  - to: bench_donors
    on: {donor_id: id}
    relationship: many_to_one
`;

async function waitForRefresh(id) {
  for (;;) {
    const dataset = await api("GET", `/api/datasets/${id}`);
    if (dataset.lastJob?.status === "succeeded") return dataset;
    if (dataset.lastJob?.status === "failed") throw new Error(`refresh failed: ${dataset.lastJob.error}`);
    await sleep(1000);
  }
}

// ---- a typical dashboard ---------------------------------------------------------------
const DASHBOARD_TITLE = "Benchmark dashboard";
function dashboardSpec() {
  const g = "bench_gifts";
  const kpi = (id, x, measure) => ({
    id, visual: "kpi", position: { x, y: 0, w: 3, h: 3 }, dataset: g,
    bindings: { value: [`${g}.${measure}`] }, config: { comparison: "previous_year" },
  });
  return {
    specVersion: 1,
    title: DASHBOARD_TITLE,
    layout: { columns: 12, rowHeight: 40 },
    filters: [
      { id: "period", type: "dateRange", field: `${g}.gift_date`, label: "Period", default: { relative: "last_12_months" } },
      { id: "region", type: "multiSelect", field: `${g}.region`, label: "Region" },
    ],
    items: [
      kpi("total", 0, "total_amount"),
      kpi("count", 3, "row_count"),
      kpi("donors", 6, "donors"),
      kpi("online", 9, "online_amount"),
      {
        id: "by_month", visual: "line", position: { x: 0, y: 3, w: 8, h: 7 }, dataset: g,
        bindings: { x: [{ field: `${g}.gift_date`, grain: "month" }], y: [`${g}.total_amount`], series: [`${g}.channel`] },
      },
      {
        id: "by_channel", visual: "pie", position: { x: 8, y: 3, w: 4, h: 7 }, dataset: g,
        bindings: { category: [`${g}.channel`], value: [`${g}.total_amount`] },
      },
      {
        id: "by_fund", visual: "bar", position: { x: 0, y: 10, w: 6, h: 7 }, dataset: g,
        bindings: { category: [`${g}.fund`], value: [`${g}.total_amount`, `${g}.row_count`] },
        sort: [{ field: `${g}.total_amount`, dir: "desc" }],
      },
      {
        id: "by_tier", visual: "table", position: { x: 6, y: 10, w: 6, h: 7 }, dataset: g,
        bindings: { columns: ["bench_donors.tier", `${g}.total_amount`, `${g}.donors`] },
      },
    ],
    interactions: { crossFilter: true },
  };
}

/** Open a dashboard as the browser does: the spec, then every item's data, six at a time. */
async function loadDashboard(id, state) {
  const start = performance.now();
  const dashboard = await api("GET", `/api/dashboards/${id}`);
  const items = [...dashboard.spec.items];
  const timings = [];
  const worker = async () => {
    while (items.length > 0) {
      const item = items.shift();
      const t = performance.now();
      const body = await api("POST", `/api/dashboards/${id}/data`, { itemId: item.id, source: "draft", state });
      timings.push({ item: item.id, wall: ms(t), cacheHit: body.cacheHit });
    }
  };
  await Promise.all(Array.from({ length: 6 }, worker));
  return { total: ms(start), slowest: Math.max(...timings.map((t) => t.wall)), hits: timings.filter((t) => t.cacheHit).length, items: timings.length };
}

async function main() {
  // 0. The test stack, from nothing.
  stack.compose(STACK, ["down", "-v", "--remove-orphans"]);
  stack.must(STACK, ["up", "-d", ...(args.includes("--no-build") ? [] : ["--build"]), "--wait"]);
  const config = await api("GET", "/api/config");
  console.log(`Opsis ${config.version} at ${BASE}, job runner: ${config.jobRunner}, rows: ${ROWS.toLocaleString()}`);
  const sampler = sampleMemory();

  // 1. Source data. Dates span three years; the timestamp column is timezone-aware.
  let start = performance.now();
  psql(`CREATE SCHEMA bench;
    CREATE TABLE bench.donors AS SELECT n AS id, 'Donor ' || n AS name,
      (ARRAY['major','regular','regular','lapsed'])[1 + n % 4] AS tier FROM generate_series(1, 200000) n;
    ALTER TABLE bench.donors ADD PRIMARY KEY (id);
    CREATE TABLE bench.gifts AS SELECT n::int AS id, (1 + (n * 7919) % 200000)::int AS donor_id,
      (ARRAY['Scholarships','Medical Research','Library','Student Hardship','Sport','Arts','Alumni','General'])[(1 + n % 8)::int] AS fund,
      (ARRAY['VIC','NSW','QLD','WA','SA','TAS'])[(1 + n % 6)::int] AS region,
      (ARRAY['online','mail','event','phone'])[(1 + n % 4)::int] AS channel,
      round((5 + (n * 37) % 4995 + (n % 100) / 100.0)::numeric, 2) AS amount,
      CURRENT_DATE - ((n * 13) % 1095)::int AS gift_date,
      now() - make_interval(secs => ((n * 19) % 94608000)::double precision) AS created_at
      FROM generate_series(1::bigint, ${ROWS}::bigint) n;
    ALTER TABLE bench.gifts ADD PRIMARY KEY (id);`);
  console.log(`\nSource tables created in Postgres: ${(ms(start) / 1000).toFixed(1)} s`);

  // 2. Ingestion.
  const connection = await api("POST", "/api/connections", {
    name: "Benchmark source",
    connectorId: "postgres",
    config: { host: "postgres", database: PG_DB, user: PG_USER, password: PG_PASSWORD },
  });
  resetPeak();
  start = performance.now();
  const donors = await api("POST", "/api/datasets", { name: "bench_donors", connectionId: connection.id, stream: "bench.donors" });
  const gifts = await api("POST", "/api/datasets", { name: "bench_gifts", connectionId: connection.id, stream: "bench.gifts" });
  await waitForRefresh(donors.id);
  const loaded = await waitForRefresh(gifts.id);
  const ingestSeconds = ms(start) / 1000;
  console.log(
    `Ingestion (${loaded.snapshot.rowCount.toLocaleString()} + 200,000 rows): ${ingestSeconds.toFixed(1)} s, ` +
      `${Math.round(loaded.snapshot.rowCount / loaded.lastJob.result.seconds).toLocaleString()} rows/s for gifts, ` +
      `snapshot ${(loaded.snapshot.sizeBytes / 1048576).toFixed(0)} MB, ` +
      `peak memory during ingestion: ${JSON.stringify(Object.fromEntries(Object.entries(peak).map(([k, v]) => [k, Math.round(v) + " MB"])))}`,
  );
  await api("PUT", "/api/models/bench_gifts", { yaml: GIFTS_MODEL });

  // 3. Five queries, cold then warm.
  resetPeak();
  console.log("\nQuery                                              cold wall  cold engine  warm wall  rows");
  for (const [name, query] of Object.entries(QUERIES)) {
    const cold = await timedQuery(query);
    const warm = await timedQuery(query);
    if (cold.cacheHit || !warm.cacheHit) throw new Error(`unexpected cache state for ${name}`);
    console.log(`${name.padEnd(50)} ${String(cold.wall).padStart(7)} ms ${String(cold.engine).padStart(9)} ms ${String(warm.wall).padStart(7)} ms ${String(cold.rows).padStart(5)}`);
  }

  // 3b. A typical dashboard, opened three times.
  const made = await api("POST", "/api/dashboards", { title: DASHBOARD_TITLE, spec: dashboardSpec() });
  console.log("\nDashboard load (8 items, 4 with a comparison)     total      slowest item  cached items");
  const loads = [
    ["cold: first open", undefined],
    ["warm: opened again", undefined],
    ["after a filter change (two regions)", { filters: { region: ["VIC", "NSW"] } }],
    ["that filter, warm", { filters: { region: ["VIC", "NSW"] } }],
  ];
  for (const [name, state] of loads) {
    const load = await loadDashboard(made.id, state);
    console.log(`${name.padEnd(50)} ${String(load.total).padStart(5)} ms ${String(load.slowest).padStart(9)} ms ${String(load.hits).padStart(8)}/${load.items}`);
  }

  // 4. Ten different (uncached) queries at once, while timing /healthz.
  const regions = ["VIC", "NSW", "QLD", "WA", "SA", "TAS"];
  const burst = (round) =>
    Array.from({ length: 10 }, (_, i) => ({
      dataset: "bench_gifts",
      dimensions: i % 2 ? ["bench_gifts.fund", "bench_gifts.channel"] : ["bench_donors.tier"],
      measures: ["bench_gifts.total_amount", "bench_gifts.donors"],
      timeDimension: { field: "bench_gifts.gift_date", grain: i % 3 ? "month" : "week" },
      filters: [{ field: "bench_gifts.region", op: "neq", values: [regions[(i + round) % 6]] }, { field: "bench_gifts.total_amount", op: "gt", values: [round * 10 + i] }],
    }));
  for (const round of [1, 2]) {
    const health = [];
    let running = true;
    const pinger = (async () => {
      while (running) {
        const t = performance.now();
        await fetch(`${BASE}/healthz`);
        health.push(ms(t));
        await sleep(50);
      }
    })();
    start = performance.now();
    const results = await Promise.all(burst(round).map(timedQuery));
    const total = ms(start);
    running = false;
    await pinger;
    const walls = results.map((r) => r.wall).sort((a, b) => a - b);
    const engines = results.map((r) => r.engine);
    console.log(
      `\n10 concurrent queries, round ${round}: all done in ${total} ms; per request wall min ${walls[0]}, median ${walls[5]}, max ${walls[9]} ms; ` +
        `engine time max ${Math.max(...engines)} ms; cache hits ${results.filter((r) => r.cacheHit).length}/10\n` +
        `  /healthz during the burst: ${health.length} checks, median ${health.sort((a, b) => a - b)[Math.floor(health.length / 2)]} ms, max ${Math.max(...health)} ms`,
    );
  }
  const peakMemory = async () => {
    await sleep(1500); // the last samples
    return JSON.stringify(Object.fromEntries(Object.entries(peak).map(([k, v]) => [k, Math.round(v) + " MB"])));
  };
  // 4b. The same again, with a rule on donors that the admin is not exempt from.
  if (RULE) {
    console.log(`\nPeak memory during the queries above: ${await peakMemory()}`);
    const count = { dataset: "bench_gifts", measures: ["bench_gifts.row_count"] };
    const before = (await api("POST", "/api/query", { query: count })).rows[0][0];
    const rule = { filter: "tier <> 'lapsed'", applies_to_roles: ["viewer", "editor", "admin"] };
    await api("PATCH", "/api/model", { ops: [{ op: "add", path: "/tables/bench_donors/rls", value: [rule] }] });
    const after = (await api("POST", "/api/query", { query: count })).rows[0][0];
    if (!(after < before)) throw new Error(`the rule on donors hid no gifts (${after} of ${before})`);
    console.log(
      `\nWith a rule on bench_donors (tier <> 'lapsed', admins included): ${Number(after).toLocaleString()} of ` +
        `${Number(before).toLocaleString()} gifts visible, each read through its donor`,
    );
    resetPeak();
    console.log("Query                                              cold wall  cold engine  warm wall  rows");
    for (const [name, query] of Object.entries(QUERIES)) {
      const cold = await timedQuery(query);
      const warm = await timedQuery(query);
      if (cold.cacheHit || !warm.cacheHit) throw new Error(`unexpected cache state for ${name}`);
      console.log(`${name.padEnd(50)} ${String(cold.wall).padStart(7)} ms ${String(cold.engine).padStart(9)} ms ${String(warm.wall).padStart(7)} ms ${String(cold.rows).padStart(5)}`);
    }
    console.log("\nDashboard load, with the rule                      total      slowest item  cached items");
    for (const [name, state] of loads) {
      const load = await loadDashboard(made.id, state);
      console.log(`${name.padEnd(50)} ${String(load.total).padStart(5)} ms ${String(load.slowest).padStart(9)} ms ${String(load.hits).padStart(8)}/${load.items}`);
    }
  }

  console.log(`\nPeak memory during ${RULE ? "the queries with the rule" : "queries"}: ${await peakMemory()}`);

  sampling = false;
  await sampler;
}

main()
  .catch((error) => {
    console.error(error);
    process.exitCode = 1;
  })
  .finally(() => {
    sampling = false;
    if (KEEP) {
      console.log(`\nThe test stack is still running at ${BASE} (project ${stack.project}). Remove it with: node scripts/benchmark.mjs --remove`);
    } else {
      stack.remove(STACK);
      console.log("The test stack was removed, data and all.");
    }
  });
