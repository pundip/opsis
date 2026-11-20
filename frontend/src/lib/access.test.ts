import { describe, expect, it } from "vitest";
import { type RestrictedData, type Share, exposuresOf, outsideOf } from "./access";

const donors: RestrictedData = {
  dataset: "donors",
  label: "Donors",
  owner: { id: "ann", name: "Ann" },
  inDraft: true,
  inPublished: true,
  canOpenUp: false,
  insideUserIds: ["ann", "ben"],
  insideGroupIds: ["finance"],
};
const share = (grantee: string, role: Share["role"], name = grantee): Share => ({
  grantee,
  role,
  name,
  kind: grantee === "everyone" ? "everyone" : grantee.startsWith("group:") ? "group" : "user",
});

describe("restricted data in the share dialog", () => {
  it("knows who is outside a dataset's list", () => {
    expect(outsideOf(donors, "user:ben")).toBe(false);
    expect(outsideOf(donors, "group:finance")).toBe(false);
    expect(outsideOf(donors, "user:vic")).toBe(true);
    expect(outsideOf(donors, "group:board")).toBe(true);
    expect(outsideOf(donors, "everyone")).toBe(true);
  });

  it("blocks new outsiders unless you may open the data up, and only warns about existing ones", () => {
    const saved = [share("user:vic", "viewer", "Vic")];
    const proposed = [...saved, share("everyone", "viewer", "Everyone"), share("group:finance", "viewer")];
    const [exposure] = exposuresOf([donors], proposed, saved);
    expect(exposure!.outside).toEqual([
      { name: "Vic", isNew: false },
      { name: "Everyone", isNew: true },
    ]);
    expect(exposure!.blocked).toBe(true);
    expect(exposuresOf([{ ...donors, canOpenUp: true }], proposed, saved)[0]!.blocked).toBe(false);
    expect(exposuresOf([donors], saved, saved)[0]!.blocked).toBe(false); // nothing new
  });

  it("counts drafts only for editors", () => {
    const draftOnly = { ...donors, inPublished: false };
    expect(exposuresOf([draftOnly], [share("user:vic", "viewer")], [])).toEqual([]);
    const upgraded = exposuresOf([draftOnly], [share("user:vic", "editor")], [share("user:vic", "viewer")]);
    expect(upgraded[0]!.outside[0]!.isNew).toBe(true); // newly sees the draft
  });
});
