// The Model view's canvas (V2_DESIGN 1.3), loaded only with the Model view: tables
// as cards, relationships as edges with 1 and * at the ends, an arrow for each way
// filters travel, dashed when inactive. Drag a field onto a field of another table
// to relate the tables; drag a card by its header to move it.
import {
  Background,
  BaseEdge,
  type Connection,
  ConnectionMode,
  Controls,
  type EdgeProps,
  Handle,
  MiniMap,
  type NodeProps,
  type NodeTypes,
  type EdgeTypes,
  Panel,
  Position,
  ReactFlow,
  ReactFlowProvider,
  getBezierPath,
  useNodesState,
  useReactFlow,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { createContext, memo, useCallback, useContext, useEffect, useMemo, useRef } from "react";
import {
  TABLE_HANDLE,
  type RelationshipLine,
  type TableCard,
  autoLayout,
  cardsOf,
  columnOf,
  edgesOf,
  handleId,
  layoutOf,
  nodesOf,
  placementsOf,
  typeGlyph,
} from "../../lib/modelGraph";
import { formatNumber } from "../../lib/format";
import { type RelationshipEnd, type WorkspaceModel, useSaveLayout } from "../../lib/workspaceModel";
import { Button, ErrorNote } from "../ui";

interface CanvasActions {
  selectedTable: string | null;
  editable: boolean;
  editTable: (name: string) => void;
  toggleCollapsed: (name: string) => void;
}

const Actions = createContext<CanvasActions>({
  selectedTable: null,
  editable: false,
  editTable: () => {},
  toggleCollapsed: () => {},
});

const TableNode = memo(function TableNode({ data }: NodeProps<TableCard>) {
  const actions = useContext(Actions);
  const active = actions.selectedTable === data.name;
  return (
    <div
      className={`w-[248px] rounded-lg border bg-white text-zinc-900 shadow-sm dark:bg-zinc-900 dark:text-zinc-100 ${
        active ? "border-brand ring-2 ring-brand/40" : "border-zinc-300 dark:border-zinc-700"
      }`}
      data-table={data.name}
    >
      <div className="model-card-header relative flex cursor-grab items-start gap-2 rounded-t-lg border-b border-zinc-200 bg-zinc-50 px-3 py-2 dark:border-zinc-800 dark:bg-zinc-800/60">
        <Handle type="source" id={handleId(TABLE_HANDLE, "l")} position={Position.Left} isConnectable={false} className="model-anchor" />
        <Handle type="source" id={handleId(TABLE_HANDLE, "r")} position={Position.Right} isConnectable={false} className="model-anchor" />
        <div className="min-w-0 flex-1">
          <p className="truncate text-sm font-semibold" title={data.label}>
            {data.label}
          </p>
          <p className="truncate text-xs text-zinc-500">
            <span className="font-mono">{data.name}</span>
            {data.rowCount !== null && ` · ${formatNumber(data.rowCount)} rows`}
            {!data.hasData && " · no data yet"}
          </p>
          {(data.restricted || data.dateTable || data.hidden || data.generated) && (
            <p className="mt-1 flex flex-wrap gap-1 text-[10px] font-medium uppercase tracking-wide">
              {data.dateTable && <span className="rounded bg-sky-100 px-1 text-sky-800 dark:bg-sky-950 dark:text-sky-300">Date table</span>}
              {data.restricted && <span className="rounded bg-amber-100 px-1 text-amber-900 dark:bg-amber-950 dark:text-amber-300">Restricted</span>}
              {data.hidden && <span className="rounded bg-zinc-200 px-1 text-zinc-700 dark:bg-zinc-700 dark:text-zinc-200">Hidden</span>}
              {data.generated && <span className="rounded bg-zinc-200 px-1 text-zinc-700 dark:bg-zinc-700 dark:text-zinc-200">Starter</span>}
            </p>
          )}
        </div>
        <div className="nodrag flex shrink-0 gap-0.5">
          <button
            type="button"
            className="rounded px-1 text-xs text-zinc-600 hover:bg-zinc-200 dark:text-zinc-300 dark:hover:bg-zinc-700"
            aria-label={`${data.collapsed ? "Show all fields of" : "Show only the keys of"} ${data.label}`}
            title={data.collapsed ? "Show all fields" : "Show only the keys"}
            disabled={!actions.editable}
            onClick={() => actions.toggleCollapsed(data.name)}
          >
            {data.collapsed ? "+" : "−"}
          </button>
          <button
            type="button"
            className="rounded px-1 text-xs font-medium text-brand hover:bg-zinc-200 dark:hover:bg-zinc-700"
            aria-label={`Edit table ${data.label}`}
            onClick={() => actions.editTable(data.name)}
          >
            Edit
          </button>
        </div>
      </div>
      <ul className="py-1">
        {data.fields.map((field) => (
          <li
            key={field.name}
            className={`relative flex h-6 items-center gap-2 px-3 text-xs ${field.hidden ? "text-zinc-400 dark:text-zinc-500" : ""}`}
          >
            <Handle type="source" id={handleId(field.name, "l")} position={Position.Left} isConnectable={false} className="model-anchor" />
            <span className="w-8 shrink-0 font-mono text-[10px] text-zinc-500" title={field.type}>
              {typeGlyph(field.type)}
            </span>
            <span className="min-w-0 flex-1 truncate font-mono">{field.name}</span>
            {field.key && <span className="shrink-0 rounded bg-zinc-200 px-1 text-[10px] dark:bg-zinc-700">key</span>}
            {field.joined && !field.key && <span className="shrink-0 text-[10px] text-zinc-500">joined</span>}
            <Handle
              type="source"
              id={handleId(field.name, "r")}
              position={Position.Right}
              isConnectable={actions.editable}
              className="model-field-handle"
              title={`${data.name}.${field.name} (${field.type}): drag onto a field of another table to relate them`}
              aria-label={`Field ${data.name}.${field.name}`}
            />
          </li>
        ))}
      </ul>
      {data.more > 0 && (
        <p className="border-t border-zinc-200 px-3 py-1 text-xs text-zinc-500 dark:border-zinc-800">
          {data.more} more {data.more === 1 ? "field" : "fields"}
          {data.collapsed ? " (keys only)" : ""}
        </p>
      )}
    </div>
  );
});

function RelationshipEdge({
  sourceX,
  sourceY,
  targetX,
  targetY,
  sourcePosition,
  targetPosition,
  data,
  selected,
}: EdgeProps<RelationshipLine>) {
  const [path, middleX, middleY] = getBezierPath({
    sourceX,
    sourceY,
    sourcePosition,
    targetX,
    targetY,
    targetPosition,
  });
  const rel = data?.relationship;
  if (!rel) return null;
  // Filters travel from the one side (target) to the many side (source).
  const angle = (Math.atan2(sourceY - targetY, sourceX - targetX) * 180) / Math.PI;
  const end = (x: number, y: number, position: Position, text: string) => {
    const right = position === Position.Right;
    return (
      <text x={x + (right ? 8 : -8)} y={y - 6} textAnchor={right ? "start" : "end"} className="model-edge-end">
        {text}
      </text>
    );
  };
  return (
    <>
      <BaseEdge
        path={path}
        interactionWidth={24}
        style={{ strokeDasharray: rel.active ? undefined : "6 4", strokeWidth: selected ? 2.5 : 1.5 }}
      />
      <g transform={`translate(${middleX} ${middleY}) rotate(${angle})`} className="model-edge-arrow">
        {rel.crossFilter === "both" ? (
          <>
            <path d="M 3 -4 L 10 0 L 3 4 z" />
            <path d="M -3 -4 L -10 0 L -3 4 z" />
          </>
        ) : (
          <path d="M -4 -4 L 4 0 L -4 4 z" />
        )}
      </g>
      {end(sourceX, sourceY, sourcePosition, rel.cardinality === "one_to_one" ? "1" : "*")}
      {end(targetX, targetY, targetPosition, "1")}
    </>
  );
}

const nodeTypes: NodeTypes = { table: TableNode };
const edgeTypes: EdgeTypes = { relationship: RelationshipEdge };

export interface ModelCanvasProps {
  model: WorkspaceModel;
  selectedTable: string | null;
  selectedRelationship: string | null;
  editable: boolean;
  onPair: (pair: { from: RelationshipEnd; to: RelationshipEnd }) => void;
  onTable: (name: string) => void;
  onRelationship: (id: string) => void;
  onBackground: () => void;
}

function Canvas({
  model,
  selectedTable,
  selectedRelationship,
  editable,
  onPair,
  onTable,
  onRelationship,
  onBackground,
}: ModelCanvasProps) {
  const flow = useReactFlow<TableCard, RelationshipLine>();
  const saveLayout = useSaveLayout();
  const { mutate: mutateLayout } = saveLayout;
  const cards = useMemo(() => cardsOf(model), [model]);
  const placed = useMemo(() => placementsOf(model, cards), [model, cards]);
  const [nodes, setNodes, onNodesChange] = useNodesState<TableCard>(nodesOf(cards, placed));
  useEffect(() => setNodes(nodesOf(cards, placed)), [cards, placed, setNodes]);
  const latest = useRef(nodes);
  latest.current = nodes;
  const edges = useMemo(() => {
    const positions = Object.fromEntries(nodes.map((node) => [node.id, node.position]));
    return edgesOf(model, cards, positions, selectedRelationship);
  }, [model, cards, nodes, selectedRelationship]);

  const save = useCallback(
    (next: TableCard[]) => {
      if (editable) mutateLayout(layoutOf(next));
    },
    [editable, mutateLayout],
  );

  const actions = useMemo<CanvasActions>(
    () => ({
      selectedTable,
      editable,
      editTable: onTable,
      toggleCollapsed: (name) =>
        save(
          latest.current.map((node) =>
            node.id === name ? { ...node, data: { ...node.data, collapsed: !node.data.collapsed } } : node,
          ),
        ),
    }),
    [selectedTable, editable, onTable, save],
  );

  const connect = useCallback(
    (connection: Connection) => {
      const from = columnOf(connection.sourceHandle);
      const to = columnOf(connection.targetHandle);
      if (!from || !to || connection.source === connection.target) return;
      onPair({
        from: { table: connection.source, columns: [from] },
        to: { table: connection.target, columns: [to] },
      });
    },
    [onPair],
  );

  const arrange = () => {
    const next = nodesOf(cards, autoLayout(cards, Object.values(model.document.relationships)));
    setNodes(next);
    save(next);
    requestAnimationFrame(() => void flow.fitView({ padding: 0.15, duration: 200 }));
  };

  return (
    <Actions.Provider value={actions}>
      <ReactFlow<TableCard, RelationshipLine>
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        edgeTypes={edgeTypes}
        onNodesChange={onNodesChange}
        onConnect={connect}
        isValidConnection={(connection) =>
          connection.source !== connection.target &&
          columnOf(connection.sourceHandle) !== null &&
          columnOf(connection.targetHandle) !== null
        }
        onNodeDragStop={(_event, _node, dragged) => {
          const moved = new Map(dragged.map((node) => [node.id, node.position]));
          save(nodes.map((node) => ({ ...node, position: moved.get(node.id) ?? node.position })));
        }}
        onNodeClick={(_event, node) => onTable(node.id)}
        onEdgeClick={(_event, edge) => onRelationship(edge.id)}
        onPaneClick={onBackground}
        connectionMode={ConnectionMode.Loose}
        connectionRadius={28}
        connectOnClick={false}
        nodesConnectable={editable}
        nodesDraggable={editable}
        deleteKeyCode={null}
        selectionKeyCode={null}
        multiSelectionKeyCode={null}
        fitView
        fitViewOptions={{ padding: 0.15 }}
        minZoom={0.2}
        maxZoom={2}
      >
        <Background gap={24} />
        <Controls showInteractive={false} />
        <MiniMap pannable zoomable ariaLabel="Overview of the model" className="hidden sm:block" />
        {editable && (
          <Panel position="top-left" className="flex flex-wrap items-center gap-2">
            <Button onClick={arrange} title="Place the tables left to right, each relationship's many side first">
              Auto layout
            </Button>
            {saveLayout.isError && (
              <div className="max-w-sm">
                <ErrorNote error={saveLayout.error} />
              </div>
            )}
          </Panel>
        )}
        {cards.length === 0 && (
          <Panel position="top-center">
            <p className="rounded-md border border-zinc-200 bg-white px-3 py-2 text-sm text-zinc-600 dark:border-zinc-800 dark:bg-zinc-900 dark:text-zinc-400">
              No tables yet. Add a dataset and its table appears here.
            </p>
          </Panel>
        )}
      </ReactFlow>
    </Actions.Provider>
  );
}

export default function ModelCanvas(props: ModelCanvasProps) {
  return (
    <div className="opsis-model h-full w-full">
      <ReactFlowProvider>
        <Canvas {...props} />
      </ReactFlowProvider>
    </div>
  );
}
