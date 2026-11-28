// The Model view's graph (V2_DESIGN 1.3): the workspace model's tables as cards and
// its relationships as edges, placed where the saved layout says, or by dagre.
import { Graph, layout as dagreLayout } from "@dagrejs/dagre";
import type { Edge, Node } from "@xyflow/react";
import {
  complete,
  describeRelationship,
  type FullRelationship,
  type Layout,
  type Position,
  type Relationship,
  type TableEntry,
  type WorkspaceModel,
} from "./workspaceModel";

export const CARD_WIDTH = 248;
const HEADER_HEIGHT = 58;
const ROW_HEIGHT = 24;
const FOOTER_HEIGHT = 24;
/** Fields drawn on a card. Keys and joined fields are always drawn. */
export const MAX_FIELDS = 30;
/** Edges whose fields are not on the card join the card's header instead. */
export const TABLE_HANDLE = "__table";
const GAP = 120;

export interface CardField {
  name: string;
  type: string;
  /** The dataset's primary key, or the date table's date. */
  key: boolean;
  /** A relationship joins on it. */
  joined: boolean;
  /** Every field over this column is hidden. */
  hidden: boolean;
}

export interface TableCardData extends Record<string, unknown> {
  name: string;
  label: string;
  rowCount: number | null;
  restricted: boolean;
  hasData: boolean;
  generated: boolean;
  dateTable: string | null;
  hidden: boolean;
  collapsed: boolean;
  fields: CardField[];
  /** Fields not drawn on the card. */
  more: number;
  measures: number;
}

export interface RelationshipEdgeData extends Record<string, unknown> {
  id: string;
  relationship: FullRelationship;
}

export type TableCard = Node<TableCardData, "table">;
export type RelationshipLine = Edge<RelationshipEdgeData, "relationship">;

export function handleId(column: string, side: "l" | "r"): string {
  return `${column}-${side}`;
}

/** The column a handle stands for, or null for a card's header. */
export function columnOf(handle: string | null | undefined): string | null {
  if (!handle) return null;
  const column = handle.slice(0, -2);
  return column === TABLE_HANDLE ? null : column;
}

function columnsOf(entry: TableEntry): { name: string; type: string }[] {
  const seen = new Set<string>();
  const columns: { name: string; type: string }[] = [];
  for (const dimension of Object.values(entry.dimensions ?? {})) {
    if (dimension.column && !seen.has(dimension.column)) {
      seen.add(dimension.column);
      columns.push({ name: dimension.column, type: dimension.type ?? "string" });
    }
  }
  return columns;
}

/** One card per table of the model, with the fields worth drawing. */
export function cardsOf(model: WorkspaceModel): TableCardData[] {
  const info = new Map(model.tables.map((table) => [table.name, table]));
  const joined = new Map<string, Set<string>>();
  for (const rel of Object.values(model.document.relationships)) {
    for (const end of [rel.from, rel.to]) {
      const columns = joined.get(end.table) ?? new Set<string>();
      end.columns.forEach((column) => columns.add(column));
      joined.set(end.table, columns);
    }
  }
  return Object.entries(model.document.tables).map(([name, entry]) => {
    const table = info.get(name);
    const keys = new Set(table?.primaryKey ?? []);
    if (entry.dateTable) keys.add(entry.dateTable.column);
    const joins = joined.get(name) ?? new Set<string>();
    const dimensions = Object.values(entry.dimensions ?? {});
    const columns = table?.hasData ? table.columns : columnsOf(entry);
    const all = columns.map((column) => {
      const over = dimensions.filter((dimension) => dimension.column === column.name);
      return {
        name: column.name,
        type: column.type,
        key: keys.has(column.name),
        joined: joins.has(column.name),
        hidden: over.length > 0 && over.every((dimension) => dimension.hidden),
      };
    });
    const collapsed = model.layout.tables?.[name]?.collapsed ?? false;
    const needed = all.filter((field) => field.key || field.joined);
    const room = collapsed ? 0 : Math.max(0, MAX_FIELDS - needed.length);
    const drawn = new Set([
      ...needed.map((field) => field.name),
      ...all
        .filter((field) => !field.key && !field.joined)
        .slice(0, room)
        .map((field) => field.name),
    ]);
    const fields = all.filter((field) => drawn.has(field.name)); // in the dataset's order
    return {
      name,
      label: entry.label || table?.label || name,
      rowCount: table?.rowCount ?? null,
      restricted: table?.restricted ?? false,
      hasData: table?.hasData ?? false,
      generated: table?.generated ?? false,
      dateTable: entry.dateTable?.column ?? null,
      hidden: entry.hidden ?? false,
      collapsed,
      fields,
      more: all.length - fields.length,
      measures: Object.keys(entry.measures ?? {}).length,
    };
  });
}

function cardHeight(card: TableCardData): number {
  return HEADER_HEIGHT + card.fields.length * ROW_HEIGHT + (card.more ? FOOTER_HEIGHT : 0) + 8;
}

/** Places the cards left to right: a relationship's many side left of its one side. */
export function autoLayout(
  cards: TableCardData[],
  relationships: Relationship[],
): Record<string, Position> {
  const graph = new Graph();
  graph.setGraph({ rankdir: "LR", nodesep: 40, ranksep: GAP, marginx: 0, marginy: 0 });
  graph.setDefaultEdgeLabel(() => ({}));
  for (const card of cards) graph.setNode(card.name, { width: CARD_WIDTH, height: cardHeight(card) });
  for (const rel of relationships) {
    if (graph.hasNode(rel.from.table) && graph.hasNode(rel.to.table)) {
      graph.setEdge(rel.from.table, rel.to.table);
    }
  }
  dagreLayout(graph);
  const placed: Record<string, Position> = {};
  for (const card of cards) {
    const centre = graph.node(card.name); // dagre gives centres
    placed[card.name] = {
      x: Math.round(centre.x - CARD_WIDTH / 2),
      y: Math.round(centre.y - cardHeight(card) / 2),
    };
  }
  return placed;
}

/** Saved places, and the tables without one laid out to the right of the others. */
export function placementsOf(model: WorkspaceModel, cards: TableCardData[]): Record<string, Position> {
  const saved = model.layout.tables ?? {};
  const placed: Record<string, Position> = {};
  const unplaced: TableCardData[] = [];
  for (const card of cards) {
    const at = saved[card.name];
    if (at) placed[card.name] = { x: at.x, y: at.y };
    else unplaced.push(card);
  }
  if (unplaced.length) {
    const names = new Set(unplaced.map((card) => card.name));
    const among = Object.values(model.document.relationships).filter(
      (rel) => names.has(rel.from.table) && names.has(rel.to.table),
    );
    const others = Object.values(placed);
    const left = others.length ? Math.max(...others.map((p) => p.x)) + CARD_WIDTH + GAP : 0;
    const top = others.length ? Math.min(...others.map((p) => p.y)) : 0;
    for (const [name, at] of Object.entries(autoLayout(unplaced, among))) {
      placed[name] = { x: at.x + left, y: at.y + top };
    }
  }
  return placed;
}

/**
 * The edges, joined at the sides of the cards that face each other: a card's right
 * when the other card is to its right, both right when one is above the other.
 */
export function edgesOf(
  model: WorkspaceModel,
  cards: TableCardData[],
  positions: Record<string, Position>,
  selected?: string | null,
): RelationshipLine[] {
  const byName = new Map(cards.map((card) => [card.name, card]));
  return Object.entries(model.document.relationships).flatMap(([id, rel]) => {
    const from = byName.get(rel.from.table);
    const to = byName.get(rel.to.table);
    const a = positions[rel.from.table];
    const b = positions[rel.to.table];
    if (!from || !to || !a || !b) return [];
    const stacked = Math.abs(a.x - b.x) < CARD_WIDTH / 2;
    const rightward = a.x <= b.x;
    const fromSide = stacked || rightward ? "r" : "l";
    const toSide = stacked ? "r" : rightward ? "l" : "r";
    const drawn = (card: TableCardData, column: string | undefined) =>
      column !== undefined && card.fields.some((field) => field.name === column) ? column : TABLE_HANDLE;
    return [
      {
        id,
        type: "relationship" as const,
        source: rel.from.table,
        target: rel.to.table,
        sourceHandle: handleId(drawn(from, rel.from.columns[0]), fromSide),
        targetHandle: handleId(drawn(to, rel.to.columns[0]), toSide),
        selected: id === selected,
        ariaLabel: `Relationship ${id}: ${describeRelationship(rel)}`,
        data: { id, relationship: complete(rel) },
      },
    ];
  });
}

export function nodesOf(cards: TableCardData[], positions: Record<string, Position>): TableCard[] {
  return cards.map((card) => ({
    id: card.name,
    type: "table" as const,
    position: positions[card.name] ?? { x: 0, y: 0 },
    data: card,
    dragHandle: ".model-card-header",
  }));
}

/** The layout to save: every card where it is now, collapsed or not. */
export function layoutOf(nodes: TableCard[], zoom?: number): Layout {
  return {
    tables: Object.fromEntries(
      nodes.map((node) => [
        node.id,
        {
          x: Math.round(node.position.x),
          y: Math.round(node.position.y),
          ...(node.data.collapsed ? { collapsed: true } : {}),
        },
      ]),
    ),
    ...(zoom !== undefined ? { zoom: Math.min(4, Math.max(0.1, Math.round(zoom * 100) / 100)) } : {}),
  };
}

/** A short glyph for a field's type on a card. */
export function typeGlyph(type: string): string {
  switch (type) {
    case "integer":
    case "number":
      return "123";
    case "decimal":
      return "1.5";
    case "boolean":
      return "y/n";
    case "date":
      return "date";
    case "timestamp":
    case "time":
      return "time";
    default:
      return "abc";
  }
}
