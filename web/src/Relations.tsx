import {
  Background,
  Controls,
  MarkerType,
  Position,
  ReactFlow,
  type Node,
  type Edge,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import type { Point, PointView, Relation, Section } from "./types";

export function relationGroup(edge: Relation, key: string) {
  return edge.kind === "related"
    ? "相关知识"
    : edge.target === key
      ? "前置知识"
      : "后续依赖";
}

export function graphData(data: PointView) {
  const key = data.point.key;
  const groups = new Map<string, string[]>();
  for (const edge of data.relations) {
    const other = edge.source === key ? edge.target : edge.source;
    groups.set(other, [
      ...new Set([...(groups.get(other) ?? []), relationGroup(edge, key)]),
    ]);
  }
  const counts: Record<string, number> = {
    前置知识: 0,
    后续依赖: 0,
    相关知识: 0,
  };
  const nodes: Node[] = [
    {
      id: key,
      position: { x: 270, y: 80 },
      data: { label: data.point.title },
      sourcePosition: Position.Right,
      targetPosition: Position.Left,
      className: "graph-current",
    },
  ];
  for (const [id, labels] of groups) {
    const group = labels.includes("前置知识")
      ? "前置知识"
      : labels.includes("后续依赖")
        ? "后续依赖"
        : "相关知识";
    const point = data.neighbors.find((p) => p.key === id);
    nodes.push({
      id,
      position: {
        x: group === "前置知识" ? 0 : group === "后续依赖" ? 540 : 270,
        y: (group === "相关知识" ? 230 : 0) + counts[group]++ * 100,
      },
      data: {
        label: `${point?.title ?? "缺失知识点"} · ${labels.join(" / ")}`,
      },
      sourcePosition: Position.Right,
      targetPosition: Position.Left,
      className: !point ? "graph-missing" : "",
    });
  }
  const edges: Edge[] = data.relations.map((edge, i) => ({
    id: `e${i}`,
    source: edge.source,
    target: edge.target,
    type: "smoothstep",
    pathOptions: {
      offset: edge.kind === "related" ? 55 : 20,
      stepPosition: edge.kind === "related" ? 0.3 : 0.7,
    },
    label: edge.kind === "related" ? "相关" : "先修",
    markerEnd:
      edge.kind === "prerequisite"
        ? { type: MarkerType.ArrowClosed }
        : undefined,
    style: {
      stroke: edge.kind === "related" ? "#9a7caa" : "#247b70",
      strokeDasharray: edge.kind === "related" ? "5 4" : undefined,
    },
  }));
  return { nodes, edges };
}

export default function Relations({
  data,
  sections,
  select,
  refs,
}: {
  data: PointView;
  sections: Section[];
  select: (point: Point) => void;
  refs: (point: Point) => React.ReactNode;
}) {
  const graph = graphData(data);
  const titles = Object.fromEntries(sections.map((s) => [s.id, s.title]));
  return (
    <div className="relations">
      <div className="section-heading">
        <h3>关联知识</h3>
        <span className="badge">一跳关系 · 只读</span>
      </div>
      <p className="muted">
        实线箭头：前置 → 依赖；虚线：相关。点击节点查看知识点。
      </p>
      {data.relations.length > 0 ? (
        <div className="graph" aria-label="局部知识关系图">
          <ReactFlow
            key={data.point.key}
            nodes={graph.nodes}
            edges={graph.edges}
            fitView
            nodesDraggable={false}
            nodesConnectable={false}
            edgesReconnectable={false}
            deleteKeyCode={null}
            onNodeClick={(_, node) => {
              const p = [data.point, ...data.neighbors].find(
                (p) => p.key === node.id,
              );
              if (p) select(p);
            }}
          >
            <Background />
            <Controls showInteractive={false} />
          </ReactFlow>
        </div>
      ) : (
        <div className="empty compact">
          此知识点暂无已保存关系。请结合下方候选检查情况判断。
        </div>
      )}
      {["前置知识", "后续依赖", "相关知识"].map((group) => (
        <section key={group} className="relation-group">
          <h4>{group}</h4>
          {data.relations
            .filter((e) => relationGroup(e, data.point.key) === group)
            .map((edge, i) => {
              const otherKey =
                edge.source === data.point.key ? edge.target : edge.source;
              const other = data.neighbors.find((p) => p.key === otherKey);
              return (
                <article className="relation-card" key={`${otherKey}-${i}`}>
                  {other ? (
                    <>
                      <button
                        className="text-button"
                        onClick={() => select(other)}
                      >
                        {other.title} ↗
                      </button>
                      <span className="muted">
                        {titles[other.section_id] ?? other.section_id}
                      </span>
                    </>
                  ) : (
                    <strong>知识点缺失：{otherKey}</strong>
                  )}
                  <p>{edge.reason}</p>
                  <details>
                    <summary>两端知识点原文依据</summary>
                    <p>{data.point.title}</p>
                    {refs(data.point)}
                    {other && (
                      <>
                        <p>{other.title}</p>
                        {refs(other)}
                      </>
                    )}
                  </details>
                </article>
              );
            })}
          {!data.relations.some(
            (e) => relationGroup(e, data.point.key) === group,
          ) && <p className="muted">暂无已保存记录</p>}
        </section>
      ))}
      <details className="pair-list">
        <summary>候选配对检查记录（{data.pairs.length}）</summary>
        <p className="muted">
          仅列出已保存候选；未列出的配对状态未知。完成候选检查不代表发现了所有关系。
        </p>
        {!data.coverage_available && (
          <p>旧数据库缺少关系检查记录，覆盖情况未知。</p>
        )}
        {data.pairs.map((pair, i) => {
          const other = data.neighbors.find((p) => p.key === pair.other_key);
          return (
            <div className="pair" key={`${pair.task_id}-${i}`}>
              <button disabled={!other} onClick={() => other && select(other)}>
                {other?.title ?? pair.other_key}
              </button>
              <span>
                {
                  {
                    with_relations: "已确认关联",
                    without_relations: "已检查未建立关联",
                    unchecked: "尚未检查",
                  }[pair.status]
                }
              </span>
              <small>
                {pair.section_id
                  ? (titles[pair.section_id] ?? pair.section_id)
                  : "全书任务"}
              </small>
            </div>
          );
        })}
      </details>
    </div>
  );
}
