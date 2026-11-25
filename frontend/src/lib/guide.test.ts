import { describe, expect, it } from "vitest";
import { guideRoute, headingId } from "./guide";

describe("guideRoute", () => {
  it("turns guide links into routes at the root and under a base path", () => {
    expect(guideRoute("/docs/guide", "")).toBe("/docs/guide");
    expect(guideRoute("/docs/guide/05-joins#two-joins-away", "")).toBe(
      "/docs/guide/05-joins#two-joins-away",
    );
    expect(guideRoute("/opsis/docs/guide", "/opsis")).toBe("/docs/guide");
    expect(guideRoute("/opsis/docs/guide#contents", "/opsis")).toBe("/docs/guide#contents");
    expect(guideRoute("/opsis/docs/guide/05-joins", "/opsis")).toBe("/docs/guide/05-joins");
  });

  it("leaves every other link to the browser", () => {
    expect(guideRoute("https://example.com/docs/guide", "")).toBeNull();
    expect(guideRoute("mailto:someone@example.com", "")).toBeNull();
    expect(guideRoute("/api/docs", "")).toBeNull();
    expect(guideRoute("/docs/guidebook", "")).toBeNull();
    expect(guideRoute("//elsewhere.test/docs/guide", "")).toBeNull();
    // Outside the base path is another app on the same domain.
    expect(guideRoute("/docs/guide", "/opsis")).toBeNull();
    expect(guideRoute("/opsisx/docs/guide", "/opsis")).toBeNull();
  });
});

describe("headingId", () => {
  it("names the heading an anchor points at", () => {
    expect(headingId("#two-joins-away")).toBe("guide-two-joins-away");
    expect(headingId("#caf%C3%A9")).toBe("guide-café");
  });

  it("is null without a usable anchor", () => {
    expect(headingId("")).toBeNull();
    expect(headingId("#")).toBeNull();
    expect(headingId("#%")).toBeNull();
  });
});
