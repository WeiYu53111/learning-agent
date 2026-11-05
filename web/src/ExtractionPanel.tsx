import { useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import type { Point } from "./types";

type Task = {
  id: string;
  status: string;
  index?: string;
  chapter?: string;
  block?: number;
  total?: number;
  phase?: string;
  error?: string;
  blocks: {
    chapter: string;
    block: number;
    quality: string;
    points: Point[];
  }[];
};
const labels: Record<string, string> = {
  queued: "等待启动",
  running: "抽取中",
  stopping: "正在停止",
  stopped: "已停止",
  completed: "已完成",
  failed: "失败",
  interrupted: "异常中断",
};

export default function ExtractionPanel({
  book,
  section,
}: {
  book: string;
  section: string;
}) {
  const client = useQueryClient();
  const [error, setError] = useState("");
  const [pending, setPending] = useState(false);
  const path = `/api/books/${encodeURIComponent(book)}/extraction`;
  const query = useQuery<Task[]>({
    queryKey: ["extraction", book],
    queryFn: async () => {
      const response = await fetch(path);
      if (!response.ok) throw new Error("抽取任务读取失败");
      return response.json();
    },
    enabled: !!book,
    refetchInterval: 2000,
  });
  const task = query.data?.[0];
  const active =
    !!task && ["queued", "running", "stopping"].includes(task.status);
  useEffect(() => {
    if (task?.status === "completed") {
      void client.invalidateQueries({ queryKey: ["catalog", book] });
      void client.invalidateQueries({ queryKey: ["chapter", book] });
    }
  }, [task?.id, task?.status, book, client]);
  async function act(suffix: string) {
    setPending(true);
    setError("");
    try {
      const response = await fetch(path + suffix, { method: "POST" });
      if (!response.ok) {
        const body = await response.json();
        throw new Error(body.detail || "操作失败");
      }
      await client.invalidateQueries({ queryKey: ["extraction", book] });
      await client.invalidateQueries({ queryKey: ["catalog", book] });
      await client.invalidateQueries({ queryKey: ["chapter", book] });
    } catch (e) {
      setError(e instanceof Error ? e.message : "操作失败");
    } finally {
      setPending(false);
    }
  }
  return (
    <section className="extraction-panel" aria-label="知识抽取控制">
      <h2>知识抽取</h2>
      <p>逐块预览，章节完成后整合。停止后从当前文本块开头重新抽取。</p>
      <div className="actions">
        <button
          disabled={pending || active}
          onClick={() => act(`?section=${encodeURIComponent(section)}`)}
        >
          抽取至当前章
        </button>
        <button disabled={pending || active} onClick={() => act("")}>
          抽取全书
        </button>
        <button
          disabled={pending || active}
          onClick={() =>
            act(`?new=true&section=${encodeURIComponent(section)}`)
          }
        >
          重抽当前章
        </button>
        <button disabled={pending || active} onClick={() => act("?new=true")}>
          重抽全书
        </button>
        {active && (
          <button
            disabled={pending || task.status === "stopping"}
            onClick={() => act(`/${task.id}/stop`)}
          >
            停止抽取
          </button>
        )}
        {task && ["stopped", "failed", "interrupted"].includes(task.status) && (
          <button disabled={pending} onClick={() => act(`?resume=${task.id}`)}>
            从{task.block ? `第 ${task.block} 块` : "检查点"}继续
          </button>
        )}
      </div>
      {(error || query.error) && (
        <p role="alert">{error || query.error?.message}</p>
      )}
      {task && (
        <>
          <p role="status">
            {labels[task.status] || task.status} · {task.chapter || "准备中"}
            {task.block && task.total
              ? ` · 第 ${Math.min(task.block, task.total)} / ${task.total} 块`
              : ""}
            {task.phase === "chapter_review" ? " · 章节整合" : ""}
          </p>
          {task.status === "stopping" && (
            <p>等待当前调用返回，不再开始下一步；当前块结果仅作临时预览。</p>
          )}
          {task.error && <p role="alert">{task.error}</p>}
          {task.status === "completed" && task.index && (
            <a
              href={`/?book=${encodeURIComponent(book)}&section=${encodeURIComponent(section)}&index=${encodeURIComponent(task.index)}`}
            >
              查看整合后的章节结果
            </a>
          )}
          {task.status !== "completed" &&
            task.blocks
              ?.filter((b) => b.chapter === section)
              .map((b) => (
                <details key={`${b.chapter}-${b.block}-${b.quality}`} open>
                  <summary>
                    第 {b.block} 块 ·{" "}
                    {b.quality === "pending"
                      ? "临时预览，恢复时替换"
                      : "文本块已完成，待章节整合"}{" "}
                    · {b.points.length} 个知识点
                  </summary>
                  {b.points.map((p) => (
                    <article key={p.key}>
                      <h3>{p.title}</h3>
                      <p>{p.description}</p>
                      <details>
                        <summary>原文引用</summary>
                        {p.source_refs.map((r, i) => (
                          <blockquote key={i}>
                            <small>{r.chunk_id}</small>
                            <p>{r.quote}</p>
                          </blockquote>
                        ))}
                      </details>
                    </article>
                  ))}
                </details>
              ))}
        </>
      )}
    </section>
  );
}
