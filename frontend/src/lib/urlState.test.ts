import { describe, expect, it } from "vitest";
import { EMPTY_STATE, type ViewState } from "./dashboard";
import { decodeViewState, encodeViewState } from "./urlState";

describe("view state in the URL", () => {
  it("round-trips filters, cross-filters and grains, unicode included", () => {
    const state: ViewState = {
      filters: { f_date: { relative: "last_fiscal_year" }, f_fund: ["Bibliothèque", "Sport"] },
      cross: [{ field: "gifts.region", values: ["VIC"], source: "v_bar", label: "VIC" }] as unknown as ViewState["cross"],
      grains: { v_trend: "month" },
    };
    const encoded = encodeViewState(state);
    expect(encoded).toMatch(/^[A-Za-z0-9_-]+$/); // safe in a URL as it is
    expect(decodeViewState(encoded)).toEqual(state);
  });

  it("keeps nothing for an empty state and ignores anything malformed", () => {
    expect(encodeViewState(EMPTY_STATE)).toBeNull();
    expect(decodeViewState(null)).toEqual(EMPTY_STATE);
    expect(decodeViewState("not base64 json")).toEqual(EMPTY_STATE);
    expect(decodeViewState(btoa("[1,2]"))).toEqual(EMPTY_STATE);
    expect(decodeViewState(btoa('{"filters": 5, "cross": "x", "grains": {"a": "month"}}'))).toEqual({
      filters: {},
      cross: [],
      grains: { a: "month" },
    });
  });
});
