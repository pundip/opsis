// A stand-in for a model in the smoke tests: a scripted dashboard builder that
// streams like Anthropic's Messages API (/v1/messages), OpenAI's Responses API
// (/v1/responses) and Ollama's own chat API (/api/chat, JSON lines). It also
// answers both providers' models endpoint (GET /v1/models), which the Account
// page's Test button and model lists use. Node only.
//
// It reads the conversation the way the assistant writes it: the <context> block
// for the dashboard's id, version and datasets, then the tool results so far to
// decide the next step. Asked to "build" a dashboard, it describes the dataset,
// lists the visuals, patches the dashboard with a KPI card and a bar chart, and
// says so, naming the last four characters of the key it was called with (or
// "no key" for Ollama), so a test can see whose key answered. Anything else gets
// a one-line answer. A key with "bad" in it is refused, as a provider would.
//
// A request that is not streamed is the Account page's "Send a test message": one
// short request, answered "OK" with its usage. A key with "broke" in it is accepted
// (its models list works) but its account cannot pay, so the test message gets the
// provider's no-credit error: Anthropic's 400, OpenAI's 429 insufficient_quota.
//
// It also refuses what the real Claude API refuses for current models, so a
// regression in the request shows up here: forced tool use, thinking turned
// off or given a budget, and sampling parameters. For OpenAI it insists on
// store: false and the whole history in `input`; for Ollama on a context size in
// each request (options.num_ctx), which a default install needs.
import { createServer } from "node:http";

const PORT = Number(process.env.PORT ?? 9500);
const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
let counter = 0;

async function json(req) {
  let raw = "";
  for await (const chunk of req) raw += chunk;
  return JSON.parse(raw || "{}");
}

function parse(text) {
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

/** The key a call carried (x-api-key, or a bearer token), or null. */
function keyOf(req) {
  if (req.headers["x-api-key"]) return req.headers["x-api-key"];
  const value = req.headers.authorization ?? "";
  return value.startsWith("Bearer ") ? value.slice(7) : null;
}

function refusedKey(key, anthropic) {
  return anthropic
    ? { type: "error", error: { type: "authentication_error", message: "invalid x-api-key" } }
    : { error: { message: `Incorrect API key provided: ${key.slice(0, 3)}****${key.slice(-4)}.`, type: "invalid_request_error", code: "invalid_api_key" } };
}

/** "Send a test message": one short request, not streamed, which the provider bills. */
function testMessage(req, res, body, anthropic) {
  const key = keyOf(req) ?? "";
  const send = (status, data) => {
    res.writeHead(status, { "Content-Type": "application/json" });
    res.end(JSON.stringify(data));
  };
  const invalid = (message) =>
    send(400, anthropic ? { type: "error", error: { type: "invalid_request_error", message } } : { error: { message, type: "invalid_request_error" } });
  if (key.includes("bad")) return send(401, refusedKey(key, anthropic));
  if (key.includes("broke")) {
    return anthropic
      ? invalid("Your credit balance is too low to access the Anthropic API. Please go to Plans & Billing to upgrade or purchase credits.")
      : send(429, { error: { message: "You exceeded your current quota, please check your plan and billing details.", type: "insufficient_quota", code: "insufficient_quota" } });
  }
  if (anthropic) {
    if (!Number.isInteger(body.max_tokens) || body.max_tokens < 1) return invalid("max_tokens: Field required");
    const usage = { input_tokens: 12, output_tokens: 1, cache_read_input_tokens: 0, cache_creation_input_tokens: 0 };
    return send(200, { id: `msg_${++counter}`, type: "message", role: "assistant", model: body.model, content: [{ type: "text", text: "OK" }], stop_reason: "max_tokens", stop_sequence: null, usage });
  }
  if (body.store !== false) return invalid("the fake model expects store: false (nothing kept at OpenAI)");
  // The Responses API takes no limit below 16.
  if (!Number.isInteger(body.max_output_tokens) || body.max_output_tokens < 16) {
    return invalid(`Invalid 'max_output_tokens': integer below minimum value. Expected a value >= 16, but got ${body.max_output_tokens} instead.`);
  }
  const message = { type: "message", id: `msg_${++counter}`, role: "assistant", status: "completed", content: [{ type: "output_text", text: "OK", annotations: [] }] };
  const usage = { input_tokens: 11, input_tokens_details: { cached_tokens: 0 }, output_tokens: 2, output_tokens_details: { reasoning_tokens: 0 }, total_tokens: 13 };
  return send(200, { id: `resp_${++counter}`, object: "response", created_at: Math.floor(Date.now() / 1000), model: body.model, status: "completed", output: [message], parallel_tool_calls: true, tool_choice: "auto", tools: [], usage });
}

/** OpenAI's history: the newest user message and the function call outputs after it. */
function openaiConversation(body) {
  const items = body.input ?? [];
  let at = -1;
  items.forEach((item, i) => {
    if (item.role === "user" && typeof item.content === "string") at = i;
  });
  const request = at < 0 ? "" : items[at].content;
  const results = items.slice(at + 1).filter((item) => item.type === "function_call_output").map((item) => parse(item.output));
  return { request, results };
}

/** The request (with its context) and the tool results after it, in either format. */
function conversation(body, anthropic) {
  const messages = body.messages ?? [];
  const isRequest = (m) =>
    m.role === "user" && (typeof m.content === "string" || (Array.isArray(m.content) && m.content.some((b) => b.type === "text")));
  let at = -1;
  messages.forEach((m, i) => {
    if (isRequest(m)) at = i;
  });
  const request = at < 0 ? "" : typeof messages[at].content === "string" ? messages[at].content : messages[at].content.map((b) => b.text ?? "").join("");
  const results = [];
  for (const m of messages.slice(at + 1)) {
    if (anthropic && m.role === "user" && Array.isArray(m.content)) {
      for (const block of m.content) if (block.type === "tool_result") results.push(parse(block.content));
    } else if (!anthropic && m.role === "tool") {
      results.push(parse(m.content));
    }
  }
  return { request, results };
}

function decide({ request, results }, key) {
  const ask = request.split("</context>").pop().trim();
  const found = /id ([0-9a-f-]{36}), version (\d+)/.exec(request);
  const listed = request.split("Datasets you may use:")[1] ?? "";
  const datasets = [...listed.matchAll(/^- (\S+) \(/gm)].map((m) => m[1]);
  if (!/build/i.test(ask)) return { parts: [["text", "I can build dashboards: ask me to build one."]] };
  const dataset = datasets.find((d) => ask.includes(d)) ?? datasets[0];
  if (!found || !dataset) return { parts: [["text", "There is no dataset to build from."]] };
  const [, id, version] = found;
  switch (results.length) {
    case 0:
      return {
        parts: [
          ["update", `Looking at ${dataset}.`],
          ["text", `I'll build a dashboard from ${dataset}.`],
          ["call", "describe_dataset", { dataset }],
        ],
      };
    case 1:
      return { parts: [["call", "list_visuals", { dataset }]] };
    case 2: {
      const example = (visual) => results[1]?.visuals?.find((v) => v.visual === visual)?.exampleItem;
      const kpi = example("kpi");
      const bar = example("bar");
      if (!kpi || !bar) return { parts: [["text", `Could not find example items: ${JSON.stringify(results[1]).slice(0, 300)}`]] };
      const items = [
        { ...kpi, id: "kpi_total", title: "Total", position: { x: 0, y: 0, w: 3, h: 3 } },
        { ...bar, id: "bar_main", title: "By category", position: { x: 0, y: 3, w: 6, h: 7 } },
      ];
      const ops = [
        { op: "replace", path: "/title", value: "Built by the assistant" },
        { op: "replace", path: "/items", value: items },
      ];
      return { parts: [["call", "patch_dashboard", { id, base_version: Number(version), ops, message: "Built by the fake model" }]] };
    }
    default: {
      const saved = results[2]?.saved === true;
      const whose = key ? `key ending ${key.slice(-4)}` : "no key";
      return { parts: [["text", saved ? `Added a KPI card and a bar chart (${whose}).` : `It did not save: ${JSON.stringify(results[2]).slice(0, 500)}`]] };
    }
  }
}

function claudeProblem(body) {
  if (body.thinking && body.thinking.type !== "adaptive") return `"thinking.type.${body.thinking.type}" is not supported for this model.`;
  if (body.tool_choice && ["any", "tool"].includes(body.tool_choice.type)) return 'tool_choice: type "tool" and "any" are not supported for this model.';
  for (const field of ["temperature", "top_p", "top_k"]) if (field in body) return `${field}: Extra inputs are not permitted`;
  if (!body.max_tokens) return "max_tokens: Field required";
  return null;
}

async function streamClaude(res, body, parts) {
  res.writeHead(200, { "Content-Type": "text/event-stream", "Cache-Control": "no-cache" });
  const send = (type, data) => res.write(`event: ${type}\ndata: ${JSON.stringify({ type, ...data })}\n\n`);
  const usage = { input_tokens: 1200, output_tokens: 1, cache_read_input_tokens: 0, cache_creation_input_tokens: 0 };
  send("message_start", { message: { id: `msg_${++counter}`, type: "message", role: "assistant", model: body.model, content: [], stop_reason: null, stop_sequence: null, usage } });
  let calls = 0;
  for (const [index, [kind, a, b]] of parts.entries()) {
    if (kind === "call") {
      calls += 1;
      send("content_block_start", { index, content_block: { type: "tool_use", id: `toolu_fake_${++counter}`, name: a, input: {} } });
      const text = JSON.stringify(b);
      for (let i = 0; i < text.length; i += 200) send("content_block_delta", { index, delta: { type: "input_json_delta", partial_json: text.slice(i, i + 200) } });
    } else {
      const thinking = kind === "update";
      send("content_block_start", { index, content_block: thinking ? { type: "thinking", thinking: "", signature: "" } : { type: "text", text: "" } });
      for (const word of a.split(/(?<= )/)) {
        send("content_block_delta", { index, delta: thinking ? { type: "thinking_delta", thinking: word } : { type: "text_delta", text: word } });
        await pause(15);
      }
      if (thinking) send("content_block_delta", { index, delta: { type: "signature_delta", signature: `fake-signature-${index}` } });
    }
    send("content_block_stop", { index });
  }
  send("message_delta", { delta: { stop_reason: calls ? "tool_use" : "end_turn", stop_sequence: null }, usage: { output_tokens: 80 } });
  send("message_stop", {});
  res.end();
}

async function streamOllama(res, body, parts) {
  res.writeHead(200, { "Content-Type": "application/x-ndjson" });
  const send = (data) => res.write(`${JSON.stringify({ model: body.model, created_at: new Date().toISOString(), ...data })}\n`);
  for (const [index, [kind, a, b]] of parts.entries()) {
    if (kind === "text") {
      for (const word of a.split(/(?<= )/)) {
        send({ message: { role: "assistant", content: word }, done: false });
        await pause(15);
      }
    } else if (kind === "call") {
      const call = { id: `call_fake_${++counter}`, function: { index, name: a, arguments: b } };
      send({ message: { role: "assistant", content: "", tool_calls: [call] }, done: false });
    }
  }
  send({
    message: { role: "assistant", content: "" },
    done: true,
    done_reason: "stop",
    prompt_eval_count: 1500,
    prompt_eval_cached_count: 1000,
    eval_count: 60,
  });
  res.end();
}

function ollamaProblem(body) {
  if (!Number.isInteger(body.options?.num_ctx)) return "the fake model expects options.num_ctx in every request";
  if (!body.messages?.length || body.messages[0].role !== "system") return "the first message should be the system prompt";
  return null;
}

function openaiProblem(body) {
  if (body.store !== false) return "the fake model expects store: false (nothing kept at OpenAI)";
  if (body.stream !== true) return "the fake model expects stream: true";
  if (!body.instructions) return "the fake model expects the system prompt in instructions";
  if (!Array.isArray(body.input) || !Array.isArray(body.tools) || body.tools.some((t) => t.type !== "function")) return "input and function tools expected";
  return null;
}

async function streamOpenAI(res, body, parts) {
  res.writeHead(200, { "Content-Type": "text/event-stream", "Cache-Control": "no-cache" });
  let sequence = 0;
  const send = (type, data) => res.write(`event: ${type}\ndata: ${JSON.stringify({ type, sequence_number: sequence++, ...data })}\n\n`);
  const base = { id: `resp_${++counter}`, object: "response", created_at: Math.floor(Date.now() / 1000), model: body.model, output: [], parallel_tool_calls: true, tool_choice: "auto", tools: [], status: "in_progress" };
  send("response.created", { response: base });
  const output = [];
  for (const [index, [kind, a, b]] of parts.entries()) {
    let done;
    if (kind === "call") {
      const item = { type: "function_call", id: `fc_${++counter}`, call_id: `call_fake_${++counter}`, name: a, arguments: "", status: "in_progress" };
      send("response.output_item.added", { output_index: index, item });
      const text = JSON.stringify(b);
      for (let i = 0; i < text.length; i += 200) send("response.function_call_arguments.delta", { item_id: item.id, output_index: index, delta: text.slice(i, i + 200) });
      done = { ...item, arguments: text, status: "completed" };
    } else {
      // A progress note is a message marked as commentary, as newer OpenAI models send it.
      const item = { type: "message", id: `msg_${++counter}`, role: "assistant", status: "in_progress", content: [], ...(kind === "update" ? { phase: "commentary" } : {}) };
      send("response.output_item.added", { output_index: index, item });
      for (const word of a.split(/(?<= )/)) {
        send("response.output_text.delta", { item_id: item.id, output_index: index, content_index: 0, delta: word, logprobs: [] });
        await pause(15);
      }
      done = { ...item, status: "completed", content: [{ type: "output_text", text: a, annotations: [] }] };
    }
    send("response.output_item.done", { output_index: index, item: done });
    output.push(done);
  }
  const usage = { input_tokens: 1300, input_tokens_details: { cached_tokens: 0, cache_write_tokens: 0 }, output_tokens: 70, output_tokens_details: { reasoning_tokens: 0 }, total_tokens: 1370 };
  send("response.completed", { response: { ...base, output, usage, status: "completed" } });
  res.end();
}

/** GET /v1/models, as Anthropic (it also sends anthropic-version) or OpenAI answers it. */
function models(req, res) {
  const anthropic = "anthropic-version" in req.headers;
  const key = keyOf(req) ?? "";
  res.writeHead(key.includes("bad") ? 401 : 200, { "Content-Type": "application/json" });
  if (key.includes("bad")) return res.end(JSON.stringify(refusedKey(key, anthropic)));
  if (anthropic) {
    const data = [
      { type: "model", id: "claude-opus-5-5", display_name: "Claude Opus 5.5", created_at: "2026-09-01T00:00:00Z" },
      { type: "model", id: "claude-haiku-5-5", display_name: "Claude Haiku 5.5", created_at: "2026-08-01T00:00:00Z" },
    ];
    return res.end(JSON.stringify({ data, has_more: false, first_id: data[0].id, last_id: data[1].id }));
  }
  const data = ["gpt-5.5", "gpt-5.5-mini", "text-embedding-3-large"].map((id) => ({ id, object: "model", created: 1767225600, owned_by: "openai" }));
  return res.end(JSON.stringify({ object: "list", data }));
}

createServer(async (req, res) => {
  try {
    const path = new URL(req.url, "http://fake").pathname;
    if (req.method === "GET" && path === "/v1/models") return models(req, res);
    if (req.method !== "POST" || !["/v1/messages", "/v1/responses", "/api/chat"].includes(path)) {
      res.writeHead(404, { "Content-Type": "application/json" });
      return res.end(JSON.stringify({ error: { message: `no route ${req.method} ${path}` } }));
    }
    const body = await json(req);
    const anthropic = path === "/v1/messages";
    const openai = path === "/v1/responses";
    // A test message has no tools; a streamed request that lost its stream flag still
    // gets the checks below.
    if ((anthropic || openai) && body.stream !== true && !body.tools) return testMessage(req, res, body, anthropic);
    const problem = anthropic ? claudeProblem(body) : openai ? openaiProblem(body) : ollamaProblem(body);
    if (problem) {
      res.writeHead(400, { "Content-Type": "application/json" });
      const error = anthropic
        ? { type: "error", error: { type: "invalid_request_error", message: problem } }
        : openai
          ? { error: { message: problem, type: "invalid_request_error" } }
          : { error: problem };
      return res.end(JSON.stringify(error));
    }
    const { parts } = decide(openai ? openaiConversation(body) : conversation(body, anthropic), keyOf(req));
    await (anthropic ? streamClaude(res, body, parts) : openai ? streamOpenAI(res, body, parts) : streamOllama(res, body, parts));
  } catch (error) {
    if (!res.headersSent) res.writeHead(500, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ error: { message: String(error) } }));
  }
}).listen(PORT, () => console.log(`fake model listening on ${PORT}`));
