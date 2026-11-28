import { describe, expect, it } from "vitest";
import {
  CARD_WIDTH,
  MAX_FIELDS,
  autoLayout,
  cardsOf,
  columnOf,
  edgesOf,
  handleId,
  layoutOf,
  nodesOf,
  placementsOf,
} from "./modelGraph";
import type { TableInfo, WorkspaceModel } from "./workspaceModel";

function info(name: string, columns: [string, string][], extra: Partial<TableInfo> = {}): TableInfo {
  return {
    name,
    datasetId: `id-${name}`,
    label: name.charAt(0).toUpperCase() + name.slice(1),
    hasData: true,
    restricted: false,
    primaryKey: [],
    columns: columns.map(([column, type]) => ({ name: column, type })),
    rowCount: 10,
    generated: false,
    ...extra,
  };
}

function model(overrides: Partial<WorkspaceModel> = {}): WorkspaceModel {
  return {
    version: 3,
    yaml: "",
    problems: [],
    warnings: [],
    updatedAt: null,
    layout: { tables: {} },
    tables: [
      info("gifts", [
        ["id", "integer"],
        ["donor_id", "integer"],
        ["amount", "decimal"],
        ["gift_date", "date"],
      ], { primaryKey: ["id"] }),
      info("donors", [
        ["id", "integer"],
        ["name", "string"],
        ["region_code", "string"],
      ], { restricted: true }),
      info("regions", [
        ["code", "string"],
        ["name", "string"],
      ]),
    ],
    document: {
      modelVersion: 2,
      tables: {
        gifts: { source: { dataset: "gifts" }, measures: { total: { agg: "sum", column: "amount" } } },
        donors: {
          source: { dataset: "donors" },
          label: "Donors",
          dimensions: { name: { column: "name", type: "string", hidden: true } },
        },
        regions: { source: { dataset: "regions" } },
      },
      relationships: {
        gifts__donors: {
          from: { table: "gifts", columns: ["donor_id"] },
          to: { table: "donors", columns: ["id"] },
        },
        donors__regions: {
          from: { table: "donors", columns: ["region_code"] },
          to: { table: "regions", columns: ["code"] },
          active: false,
        },
      },
    },
    ...overrides,
  };
}

describe("cards", () => {
  it("draws every table with its fields, keys and badges", () => {
    const cards = cardsOf(model());
    expect(cards.map((card) => card.name)).toEqual(["gifts", "donors", "regions"]);
    const gifts = cards[0]!;
    const donors = cards[1]!;
    expect(gifts.fields.map((field) => [field.name, field.key, field.joined])).toEqual([
      ["id", true, false],
      ["donor_id", false, true],
      ["amount", false, false],
      ["gift_date", false, false],
    ]);
    expect(gifts.measures).toBe(1);
    expect(donors.label).toBe("Donors");
    expect(donors.restricted).toBe(true);
    expect(donors.fields.find((field) => field.name === "name")?.hidden).toBe(true);
  });

  it("always draws keys and joined fields, even past the limit or collapsed", () => {
    const many = Array.from({ length: 50 }, (_, i): [string, string] => [`c${i}`, "string"]);
    const base = model();
    const wide = model({
      tables: [info("gifts", [...many, ["donor_id", "integer"]]), ...base.tables.slice(1)],
    });
    const gifts = cardsOf(wide)[0]!;
    expect(gifts.fields).toHaveLength(MAX_FIELDS);
    expect(gifts.fields.at(-1)?.name).toBe("donor_id"); // kept, in the dataset's order
    expect(gifts.more).toBe(51 - MAX_FIELDS);

    const collapsed = cardsOf({ ...wide, layout: { tables: { gifts: { x: 0, y: 0, collapsed: true } } } });
    expect(collapsed[0]!.fields.map((field) => field.name)).toEqual(["donor_id"]);
    expect(collapsed[0]!.collapsed).toBe(true);
  });

  it("takes a table's fields from its dimensions when it has no data yet", () => {
    const empty = model();
    empty.tables = empty.tables.filter((table) => table.name !== "regions");
    empty.document.tables.regions = {
      source: { dataset: "regions" },
      dimensions: { code: { column: "code", type: "string" } },
    };
    const regions = cardsOf(empty).find((card) => card.name === "regions");
    expect(regions?.hasData).toBe(false);
    expect(regions?.fields.map((field) => field.name)).toEqual(["code"]);
  });
});

describe("placing the cards", () => {
  it("puts the many side left of the one side", () => {
    const placed = autoLayout(cardsOf(model()), Object.values(model().document.relationships));
    expect(placed.gifts!.x).toBeLessThan(placed.donors!.x);
    expect(placed.donors!.x).toBeLessThan(placed.regions!.x);
  });

  it("keeps saved places and puts new tables to the right of them", () => {
    const saved = model({ layout: { tables: { gifts: { x: 500, y: 40 }, donors: { x: 900, y: 40 } } } });
    const placed = placementsOf(saved, cardsOf(saved));
    expect(placed.gifts).toEqual({ x: 500, y: 40 });
    expect(placed.donors).toEqual({ x: 900, y: 40 });
    expect(placed.regions!.x).toBeGreaterThan(900 + CARD_WIDTH);
  });
});

describe("edges", () => {
  it("joins the sides of the cards that face each other", () => {
    const m = model();
    const cards = cardsOf(m);
    const facing = edgesOf(m, cards, { gifts: { x: 0, y: 0 }, donors: { x: 400, y: 0 }, regions: { x: 800, y: 0 } });
    const toDonors = facing.find((edge) => edge.id === "gifts__donors");
    expect(toDonors).toMatchObject({
      source: "gifts",
      target: "donors",
      sourceHandle: handleId("donor_id", "r"),
      targetHandle: handleId("id", "l"),
      type: "relationship",
    });
    expect(toDonors?.data?.relationship).toMatchObject({ cardinality: "many_to_one", active: true });
    expect(facing.find((edge) => edge.id === "donors__regions")?.data?.relationship.active).toBe(false);

    const flipped = edgesOf(m, cards, { gifts: { x: 800, y: 0 }, donors: { x: 0, y: 0 }, regions: { x: 0, y: 400 } });
    expect(flipped.find((edge) => edge.id === "gifts__donors")).toMatchObject({
      sourceHandle: handleId("donor_id", "l"),
      targetHandle: handleId("id", "r"),
    });
    // One card above the other: both ends on the right.
    expect(flipped.find((edge) => edge.id === "donors__regions")).toMatchObject({
      sourceHandle: handleId("region_code", "r"),
      targetHandle: handleId("code", "r"),
    });
  });

  it("joins the header when a field is not on the card", () => {
    const m = model();
    m.document.relationships.gifts__donors!.to.columns = ["missing"];
    const cards = cardsOf(m);
    const edge = edgesOf(m, cards, { gifts: { x: 0, y: 0 }, donors: { x: 400, y: 0 }, regions: { x: 800, y: 0 } })[0]!;
    expect(edge.targetHandle).toBe("__table-l");
    expect(columnOf(edge.targetHandle)).toBeNull();
    expect(columnOf(edge.sourceHandle)).toBe("donor_id");
  });
});

describe("the layout to save", () => {
  it("rounds places and keeps collapsed cards collapsed", () => {
    const m = model({ layout: { tables: { donors: { x: 1, y: 2, collapsed: true } } } });
    const cards = cardsOf(m);
    const nodes = nodesOf(cards, { gifts: { x: 10.4, y: 20.6 }, donors: { x: 1, y: 2 }, regions: { x: 5, y: 5 } });
    expect(layoutOf(nodes, 0.756)).toEqual({
      tables: {
        gifts: { x: 10, y: 21 },
        donors: { x: 1, y: 2, collapsed: true },
        regions: { x: 5, y: 5 },
      },
      zoom: 0.76,
    });
  });
});
