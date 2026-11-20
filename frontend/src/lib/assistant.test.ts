import { describe, expect, it } from "vitest";
import { type Entry, type LiveEvent, NO_LIVE, applyEvent, parseEvent } from "./assistant";

const entry = (id: number, kind: Entry["kind"], data: object): Entry => ({ id, kind, data, createdAt: null }) as Entry;

describe("the assistant's event stream", () => {
  it("reads server-sent events and skips keep-alive comments", () => {
    expect(parseEvent('event: delta\ndata: {"block":0,"kind":"text","text":"Hi"}')).toEqual({
      event: "delta",
      data: { block: 0, kind: "text", text: "Hi" },
    });
    expect(parseEvent(": keep-alive")).toBeNull();
  });

  it("builds up a reply and replaces it with what was stored", () => {
    const events: LiveEvent[] = [
      { event: "start", data: { conversationId: "c", entry: entry(1, "request", { text: "Build it", selected: null }) } },
      { event: "thinking", data: { block: 0 } },
      { event: "delta", data: { block: 1, kind: "update", text: "Looking at " } },
      { event: "delta", data: { block: 1, kind: "update", text: "the data." } },
      { event: "delta", data: { block: 2, kind: "text", text: "I'll build it." } },
      { event: "call", data: { block: 3, id: "t1", tool: "describe_dataset" } },
    ];
    const live = events.reduce(applyEvent, NO_LIVE);
    expect(live.blocks).toEqual({ 1: { kind: "update", text: "Looking at the data." }, 2: { kind: "text", text: "I'll build it." } });
    expect(live.writing).toBe("describe_dataset");
    expect(live.thinking).toBe(false);

    const step = { callId: "t1", tool: "describe_dataset", title: "Describe a dataset", input: "{}" };
    const running = applyEvent(applyEvent(live, { event: "entry", data: entry(2, "update", { text: "Looking at the data." }) }), {
      event: "step_started",
      data: step,
    });
    expect(running.blocks).toEqual({}); // the stored entry took over from the live text
    expect(running.running).toEqual(step);
    const done = applyEvent(running, { event: "entry", data: entry(3, "step", { ...step, ok: true }) });
    expect(done.running).toBeNull();
    expect(done.entries.map((e) => e.id)).toEqual([1, 2, 3]);
  });

  it("drops what was shown of a call that is made again", () => {
    const live = applyEvent(NO_LIVE, { event: "delta", data: { block: 0, kind: "text", text: "Half a repl" } });
    expect(applyEvent(live, { event: "retry", data: {} }).blocks).toEqual({});
  });
});
