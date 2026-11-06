import { activeTask, labels, useExtraction } from "./useExtraction";
export default function ExtractionPanel({
  book,
  section,
}: {
  book: string;
  section: string;
}) {
  const { query, pending, error, act, open } = useExtraction(book);
  const task = query.data?.[0];
  const busy = pending || activeTask(task);
  return (
    <section className="extraction-panel" aria-label="知识抽取控制">
      <h2>知识抽取</h2>
      <p>在独立页面实时查看知识点、内容清单和原文对照。</p>
      <div className="actions">
        <button onClick={() => open(task?.id || "")}>实时抽取 · 查看内容</button>
        <button
          disabled={busy}
          onClick={() => act(`?section=${encodeURIComponent(section)}`)}
        >
          抽取至当前章
        </button>
        <button disabled={busy} onClick={() => act("")}>
          抽取全书
        </button>
        <button
          disabled={busy}
          onClick={() =>
            act(`?new=true&section=${encodeURIComponent(section)}`)
          }
        >
          重抽当前章
        </button>
        <button disabled={busy} onClick={() => act("?new=true")}>
          重抽全书
        </button>
      </div>
      {task && (
        <p>
          {labels[task.status]} · {task.chapter || "准备中"}
        </p>
      )}
      {!task && <p>暂无抽取任务。已有索引可在下方浏览章节结果。</p>}
      {(error || query.error) && (
        <p role="alert">{error || query.error?.message}</p>
      )}
    </section>
  );
}
