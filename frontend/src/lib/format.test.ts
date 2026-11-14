import { describe, expect, it } from "vitest";
import { formatBytes, formatCell, formatDuration, timeAgo, toIdentifier } from "./format";

describe("format", () => {
  it("formats sizes", () => {
    expect(formatBytes(512)).toBe("512 B");
    expect(formatBytes(1536)).toBe("1.5 KB");
    expect(formatBytes(50 * 1024 * 1024)).toBe("50 MB");
  });

  it("formats durations", () => {
    expect(formatDuration(0.2)).toBe("under 1 s");
    expect(formatDuration(42)).toBe("42 s");
    expect(formatDuration(125)).toBe("2 min 5 s");
  });

  it("describes elapsed time", () => {
    const now = new Date("2026-11-14T12:00:00Z");
    expect(timeAgo("2026-11-14T11:59:55Z", now)).toBe("just now");
    expect(timeAgo("2026-11-14T11:59:20Z", now)).toBe("40 seconds ago");
    expect(timeAgo("2026-11-14T11:59:00Z", now)).toBe("1 minute ago");
    expect(timeAgo("2026-11-14T09:00:00Z", now)).toBe("3 hours ago");
    expect(timeAgo("2026-11-11T12:00:00Z", now)).toBe("3 days ago");
    expect(timeAgo("2027-01-01T00:00:00Z", now)).toBe("just now");
  });

  it("makes dataset identifiers", () => {
    expect(toIdentifier("My Gifts 2026")).toBe("my_gifts_2026");
    expect(toIdentifier("  --Odd/Name--  ")).toBe("odd_name");
    expect(toIdentifier("2026 gifts")).toBe("t_2026_gifts");
  });

  it("formats cells", () => {
    expect(formatCell(null)).toBe("");
    expect(formatCell(false)).toBe("false");
    expect(formatCell(10.5)).toBe("10.5");
    expect(formatCell("2026-01-05T10:00:00", "timestamp")).toBe("2026-01-05 10:00:00");
  });
});
