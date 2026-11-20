// A stand-in for a model in the smoke tests: a scripted dashboard builder that
// streams like Anthropic's Messages API (/v1/messages) and like Ollama's own chat
// API (/api/chat, JSON lines). Node only.
//
// It reads the conversation the way the assistant writes it: the <context> block
// for the dashboard's id, version and datasets, then the tool results so far to
// decide the next step. Asked to "build" a dashboard, it describes the dataset,
// lists the visuals, patches the dashboard with a KPI card and a bar chart, and
// says so. Anything else gets a one-line answer.
//
// It also refuses what the real Claude API refuses for current models, so a
// regression in the request shows up here: forced tool use, thinking turned
// off or given a budget, and sampling parameters. For Ollama it insists on a
// context size in each request (options.num_ctx), which a default install needs.
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

function decide({ request, results }) {
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
      return { parts: [["text", saved ? "Added a KPI card and a bar chart." : `It did not save: ${JSON.stringify(results[2]).slice(0, 500)}`]] };
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

createServer(async (req, res) => {
  try {
    const path = new URL(req.url, "http://fake").pathname;
    if (req.method !== "POST" || !["/v1/messages", "/api/chat"].includes(path)) {
      res.writeHead(404, { "Content-Type": "application/json" });
      return res.end(JSON.stringify({ error: { message: `no route ${req.method} ${path}` } }));
    }
    const body = await json(req);
    const anthropic = path === "/v1/messages";
    const problem = anthropic ? claudeProblem(body) : ollamaProblem(body);
    if (problem) {
      res.writeHead(400, { "Content-Type": "application/json" });
      const error = anthropic ? { type: "error", error: { type: "invalid_request_error", message: problem } } : { error: problem };
      return res.end(JSON.stringify(error));
    }
    const { parts } = decide(conversation(body, anthropic));
    await (anthropic ? streamClaude(res, body, parts) : streamOllama(res, body, parts));
  } catch (error) {
    if (!res.headersSent) res.writeHead(500, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ error: { message: String(error) } }));
  }
}).listen(PORT, () => console.log(`fake model listening on ${PORT}`));
