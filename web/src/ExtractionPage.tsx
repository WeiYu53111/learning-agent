import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";
import {
  activeTask,
  extractionRequest,
  labels,
  useExtraction,
  type Task,
} from "./useExtraction";
import type { Point, Item, Chunk } from "./types";
import { Highlight } from "./SourceText";
import { BlockTiming, type Timing } from "./BlockTiming";
type Content = {
  timing?: Timing | null;
  exclusions?: {
    role: string;
    reason: string;
    chunk_id: string;
    start: number;
    end: number;
  }[];
  points: Point[];
  items: Item[];
  chunks: Chunk[];
  quality?: string;
  chapter?: string;
  block?: number;
};
type Detail = {
  task: Task;
  sections: {
    id: string;
    title: string;
    blocks: {
      number: number;
      count: number;
      quality: string;
      excluded_roles?: string[];
      timing?: Timing | null;
    }[];
  }[];
  selected: Content;
  final: Content | null;
  legacy: boolean;
};
const roleLabels: Record<string, string> = {
  publication: "出版或导入信息",
  navigation: "目录导航",
};
const blockLabels: Record<string, string> = {
  waiting: "尚未处理",
  processing: "正在处理",
  paused: "处理已中断",
  excluded: "已排除",
  block_reviewed: "文本块已完成",
  pending: "临时预览 · 恢复时替换",
};
export default function ExtractionPage() {
  const [params, setParams] = useSearchParams();
  const book = params.get("book") || "";
  const { query, path, pending, error, act, open } = useExtraction(book);
  const taskId = params.get("task") || query.data?.[0]?.id || "";
  const chapter = params.get("chapter"),
    block = params.get("block");
  const [pointKey, setPointKey] = useState("");
  const [mode, setMode] = useState("blocks");
  const [showDetail, setShowDetail] = useState(false);
  const detail = useQuery<Detail>({
    queryKey: ["extraction-detail", book, taskId, chapter, block],
    queryFn: () =>
      extractionRequest(
        `${path}/${encodeURIComponent(taskId)}${chapter && block ? `?chapter=${encodeURIComponent(chapter)}&block=${encodeURIComponent(block)}` : ""}`,
      ),
    enabled: !!book && !!taskId,
    refetchInterval: (q) =>
      !q.state.data || activeTask(q.state.data.task) ? 2000 : false,
  });
  const data = detail.data,
    task = data?.task;
  const content = mode === "final" && data?.final ? data.final : data?.selected;
  const point =
    content?.points.find((p) => p.key === pointKey) || content?.points[0];
  function select(chapterId?: string, number?: number) {
    const next = new URLSearchParams({
      view: "extraction",
      book,
      task: taskId,
    });
    if (chapterId && number) {
      next.set("chapter", chapterId);
      next.set("block", String(number));
    }
    setParams(next);
    setPointKey("");
    setMode("blocks");
  }
  return (
    <main className="extraction-page">
      <header>
        <a href={`/?book=${encodeURIComponent(book)}`}>← 返回教材工作台</a>
        <h1>实时抽取内容</h1>
        <label>
          抽取任务{" "}
          <select
            aria-label="抽取任务"
            value={taskId}
            onChange={(e) => {
              open(e.target.value);
              setPointKey("");
              setMode("blocks");
            }}
          >
            {query.data?.map((t) => (
              <option key={t.id} value={t.id}>
                {labels[t.status]} · {t.id}
              </option>
            ))}
          </select>
        </label>
        <p role="status">
          {task
            ? `${labels[task.status]} · ${task.chapter || "准备中"}${task.block && task.total ? ` · 第 ${Math.min(task.block, task.total)}${task.block_end && task.block_end > task.block ? `～${task.block_end}` : ""} / ${task.total} 块` : ""}`
            : "正在加载任务…"}
        </p>
        <div className="actions">
          <button onClick={() => select()} disabled={!chapter}>
            跟随当前抽取
          </button>
          {activeTask(task) && (
            <button
              disabled={pending || task?.status === "stopping"}
              onClick={() => act(`/${taskId}/stop`)}
            >
              停止抽取
            </button>
          )}
          {task &&
            ["stopped", "failed", "interrupted"].includes(task.status) && (
              <button
                disabled={pending}
                onClick={() => act(`?resume=${taskId}`)}
              >
                从{task.block ? `第 ${task.block} 块` : "检查点"}继续
              </button>
            )}
          <button
            aria-pressed={mode === "blocks"}
            onClick={() => setMode("blocks")}
          >
            逐块抽取记录
          </button>
          <button
            disabled={!data?.final}
            aria-pressed={mode === "final"}
            onClick={() => {
              setMode("final");
              setPointKey("");
            }}
          >
            章节整合结果
          </button>
        </div>
        <p className="muted">
          按模型调用返回更新，非逐字生成。记录来自已保存检查点，不是历史任务快照。
        </p>
        {task?.status === "stopping" && (
          <p>等待当前调用返回；下次从当前模型批次的首块重新抽取。</p>
        )}
        {(error || query.error || detail.error || task?.error) && (
          <p role="alert">
            {error ||
              detail.error?.message ||
              query.error?.message ||
              task?.error}{" "}
            <button
              onClick={() => {
                void detail.refetch();
                void query.refetch();
              }}
            >
              重试读取
            </button>
          </p>
        )}
        {data?.legacy && (
          <p>旧索引没有逐块记录，请选择“章节整合结果”查看已有内容。</p>
        )}
        {!taskId && !query.isLoading && (
          <p>暂无任务，请返回工作台启动抽取或查看已有章节索引。</p>
        )}
      </header>
      <div className="extraction-columns">
        <nav aria-label="文本块导航">
          {data?.sections.map((s) => (
            <details key={s.id} open={s.id === data.selected.chapter}>
              <summary>{s.title}</summary>
              {s.blocks.map((b) => (
                <button
                  key={b.number}
                  aria-current={
                    s.id === data.selected.chapter &&
                    b.number === data.selected.block
                      ? "true"
                      : undefined
                  }
                  onClick={() => select(s.id, b.number)}
                >
                  第 {b.number} 块 ·{" "}
                  {b.quality === "excluded"
                    ? `已排除：${b.excluded_roles?.map((r) => roleLabels[r] || r).join("、") || "非教学内容"}`
                    : ["waiting", "processing", "paused"].includes(b.quality)
                      ? blockLabels[b.quality]
                      : `${b.count} 个知识点`}
                  {b.quality !== "waiting" && (
                    <small style={{ display: "block" }}>
                      <BlockTiming timing={b.timing} />
                    </small>
                  )}
                </button>
              ))}
            </details>
          ))}
        </nav>
        <section aria-label="知识点列表">
          {mode === "blocks" && data?.selected.quality !== "waiting" && (
            <p>
              <BlockTiming timing={data?.selected.timing} detail />
            </p>
          )}
          <h2>
            {mode === "final"
              ? "章节整合结果"
              : `第 ${data?.selected.block || "—"} 块`}
          </h2>
          <p>
            {mode === "final"
              ? "章节审核后的知识点"
              : blockLabels[data?.selected.quality || "waiting"]}
          </p>
          {content?.points.map((p) => (
            <button
              className="extraction-point"
              key={p.key}
              aria-pressed={p.key === point?.key}
              onClick={() => {
                setPointKey(p.key);
                setShowDetail(true);
              }}
            >
              <strong>{p.title}</strong>
              <span>{p.description}</span>
            </button>
          ))}
          {content && !content.points.length && (
            <p>
              {mode === "final"
                ? "本章处理完成，没有生成知识点。"
                : data?.selected.quality === "excluded"
                  ? "本块已处理完成，全部内容被排除，因此没有生成知识点。"
                  : data?.selected.quality === "processing"
                    ? "当前块所属批次正在处理；知识抽取调用返回后即可看到知识点。"
                    : data?.selected.quality === "paused"
                      ? "本块所在批次尚未完成；继续任务后从该批次首块重新处理。"
                      : data?.selected.quality === "block_reviewed"
                        ? "本块已处理完成，没有生成知识点；请结合原文检查。"
                        : "本块尚未处理，暂无抽取结果。"}
            </p>
          )}
          {content?.exclusions?.map((r, i) => (
            <article key={`${r.chunk_id}-${r.start}-${i}`}>
              <h3>已排除：{roleLabels[r.role] || r.role}</h3>
              <p>{r.reason || "未记录具体原因。"}</p>
              <details>
                <summary>查看被排除的原文</summary>
                <blockquote>
                  {content.chunks
                    .find((c) => c.id === r.chunk_id)
                    ?.text.slice(r.start, r.end) || "原文暂不可用。"}
                </blockquote>
              </details>
            </article>
          ))}
          <button onClick={() => setShowDetail(true)}>查看原文与详情</button>
        </section>
        <aside
          className={
            showDetail ? "extraction-detail open" : "extraction-detail"
          }
          aria-label="知识点与原文对照"
        >
          <button className="detail-close" onClick={() => setShowDetail(false)}>
            关闭详情
          </button>
          <h2>{point?.title || "原文"}</h2>
          <p>{point?.description}</p>
          {content?.items
            .filter((i) => point && i.point_keys.includes(point.key))
            .map((i, n) => (
              <article key={n}>
                <h3>内容清单 · {i.kind}</h3>
                <p>{i.description}</p>
                {i.source_refs.map((r, j) => (
                  <blockquote key={j}>{r.quote}</blockquote>
                ))}
              </article>
            ))}
          {content?.chunks.map((c) => (
            <article key={c.id}>
              <h3>
                原文 · 第 {c.page} 页 · {c.id}
              </h3>
              <pre>
                <Highlight
                  text={c.text}
                  quote={
                    point?.source_refs.find((r) => r.chunk_id === c.id)?.quote
                  }
                />
              </pre>
            </article>
          ))}
        </aside>
      </div>
    </main>
  );
}
