import { useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";
import Relations from "./Relations";
import type {
  Book,
  Catalog,
  Chapter,
  Chunk,
  Page,
  Point,
  PointView,
  Ref,
} from "./types";

async function get<T>(path: string): Promise<T> {
  const response = await fetch(`/api${path}`);
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(
      typeof body.detail === "string"
        ? body.detail
        : "读取失败，请刷新后重试。",
    );
  }
  return response.json();
}
const encode = encodeURIComponent;
const kinds: Record<string, string> = {
  definition: "定义",
  mechanism: "机制",
  procedure: "步骤",
  boundary: "边界",
  example: "示例",
  background: "背景",
};
const statuses: Record<string, string> = {
  candidate_complete: "候选检查完成",
  partial: "部分完成",
  not_started: "尚未分析",
};

export function Highlight({ text, quote }: { text: string; quote?: string }) {
  if (!quote) return <>{text}</>;
  const start = text.indexOf(quote);
  if (start < 0 || text.indexOf(quote, start + 1) >= 0)
    return (
      <>
        <span className="notice">引用无法唯一匹配此文本块。</span>
        {text}
      </>
    );
  return (
    <>
      {text.slice(0, start)}
      <mark>{quote}</mark>
      {text.slice(start + quote.length)}
    </>
  );
}

function Source({
  book,
  initialPage,
  reference,
  clear,
}: {
  book: string;
  initialPage: number;
  reference: Ref | null;
  clear: () => void;
}) {
  const [page, setPage] = useState(initialPage);
  useEffect(() => {
    setPage(initialPage);
  }, [initialPage, book]);
  const chunk = useQuery({
    queryKey: ["chunk", book, reference?.chunk_id],
    queryFn: () =>
      get<Chunk>(
        `/books/${encode(book)}/chunks/${encode(reference!.chunk_id)}`,
      ),
    enabled: !!reference,
  });
  const shownPage = reference && chunk.data ? chunk.data.page : page;
  const source = useQuery({
    queryKey: ["page", book, shownPage],
    queryFn: () => get<Page>(`/books/${encode(book)}/pages/${shownPage}`),
  });
  useEffect(() => {
    if (source.data && reference && chunk.data)
      document
        .getElementById(`chunk-${reference.chunk_id}`)
        ?.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }, [source.data, reference, chunk.data]);
  function changePage(value: number) {
    clear();
    setPage(value);
  }
  return (
    <aside className="source-panel panel">
      <div className="section-heading">
        <h2>原文对照</h2>
        <span className="badge neutral">PDF 物理页码</span>
      </div>
      <div className="page-nav">
        <button
          aria-label="上一页"
          disabled={shownPage <= 1}
          onClick={() => changePage(shownPage - 1)}
        >
          ←
        </button>
        <label>
          第{" "}
          <input
            aria-label="原文页码"
            type="number"
            min={1}
            max={source.data?.page_count}
            value={shownPage}
            onChange={(e) => {
              const value = Number(e.target.value);
              if (value >= 1 && value <= (source.data?.page_count ?? 1))
                changePage(value);
            }}
          />{" "}
          / {source.data?.page_count ?? "…"} 页
        </label>
        <button
          aria-label="下一页"
          disabled={!source.data || shownPage >= source.data.page_count}
          onClick={() => changePage(shownPage + 1)}
        >
          →
        </button>
      </div>
      {reference && (
        <div className="quote-preview">
          <small>选中引用</small>
          <p>“{reference.quote}”</p>
        </div>
      )}
      {(chunk.error || source.error) && (
        <p role="alert" className="notice">
          {(chunk.error || source.error)?.message}
        </p>
      )}
      {source.isPending && <p className="muted">正在读取原文…</p>}
      {source.data && !source.data.chunks.length && (
        <div className="empty compact">
          {source.data.unavailable
            ? "此页不可读取，当前不支持 OCR。"
            : "此页没有已解析文本。"}
        </div>
      )}
      {source.data?.chunks.map((c) => (
        <article
          id={`chunk-${c.id}`}
          className={`source-chunk ${reference?.chunk_id === c.id ? "focused" : ""}`}
          key={c.id}
        >
          <small>{c.id}</small>
          <p>
            <Highlight
              text={c.text}
              quote={reference?.chunk_id === c.id ? reference.quote : undefined}
            />
          </p>
        </article>
      ))}
    </aside>
  );
}

export default function App() {
  const [params, setParams] = useSearchParams();
  const client = useQueryClient();
  const [reference, setReference] = useState<Ref | null>(null);
  const [search, setSearch] = useState("");
  const [kind, setKind] = useState("");
  const [tab, setTab] = useState("points");
  const [refreshing, setRefreshing] = useState(false);
  const books = useQuery({
    queryKey: ["books"],
    queryFn: () => get<Book[]>("/books"),
  });
  const book = params.get("book") ?? books.data?.[0]?.id ?? "";
  const catalog = useQuery({
    queryKey: ["catalog", book],
    queryFn: () => get<Catalog>(`/books/${encode(book)}`),
    enabled: !!book,
  });
  const index =
    params.get("index") ??
    catalog.data?.indexes.find((i) => i.pipeline === "semantic-v1")?.id ??
    "";
  const section = params.get("section") ?? catalog.data?.sections[0]?.id ?? "";
  const key = params.get("point") ?? "";
  const chapter = useQuery({
    queryKey: ["chapter", book, index, section],
    queryFn: () =>
      get<Chapter>(
        `/books/${encode(book)}/sections/${encode(section)}${index ? `?index=${encode(index)}` : ""}`,
      ),
    enabled: !!book && !!section,
  });
  const point = useQuery({
    queryKey: ["point", book, index, key],
    queryFn: () =>
      get<PointView>(
        `/books/${encode(book)}/points/${encode(key)}?index=${encode(index)}`,
      ),
    enabled: !!book && !!index && !!key,
  });
  useEffect(() => {
    setReference(null);
    setSearch("");
    setKind("");
  }, [book, index, section]);
  // Materialize defaults so refreshing and sharing retain the exact selected index.
  useEffect(() => {
    if (!catalog.data || !book || !section) return;
    if (
      !params.has("book") ||
      !params.has("section") ||
      (index && !params.has("index"))
    ) {
      const next = new URLSearchParams(params);
      next.set("book", book);
      next.set("section", section);
      if (index) next.set("index", index);
      setParams(next, { replace: true });
    }
  }, [catalog.data, book, section, index, params, setParams]);
  function navigate(values: Record<string, string>) {
    const next = new URLSearchParams({
      book,
      ...(index ? { index } : {}),
      ...(section ? { section } : {}),
    });
    for (const [name, value] of Object.entries(values)) {
      if (value) next.set(name, value);
      else next.delete(name);
    }
    setParams(next);
  }
  function select(p: Point) {
    setReference(null);
    setSearch("");
    setKind("");
    navigate({ section: p.section_id, point: p.key });
    setTab("points");
  }
  function refs(value: { source_refs: Ref[] }) {
    return (
      <div className="refs">
        {value.source_refs.map((r, i) => (
          <button
            key={`${r.chunk_id}-${i}`}
            onClick={() => setReference(r)}
            title={r.quote}
          >
            ↗ 原文 {i + 1}
          </button>
        ))}
      </div>
    );
  }
  async function refresh() {
    setRefreshing(true);
    try {
      await client.invalidateQueries();
    } finally {
      setRefreshing(false);
    }
  }
  const text = search.trim().toLocaleLowerCase();
  const matches = (value: string) => value.toLocaleLowerCase().includes(text);
  const visibleItems =
    chapter.data?.items.filter(
      (i) => (!kind || i.kind === kind) && matches(i.description),
    ) ?? [];
  const visiblePoints =
    chapter.data?.points.filter(
      (p) =>
        matches(`${p.title} ${p.description}`) &&
        (!kind ||
          chapter.data?.items.some(
            (i) => i.kind === kind && i.point_keys.includes(p.key),
          )),
    ) ?? [];
  const error = books.error || catalog.error || chapter.error;
  return (
    <div className="app">
      <header className="topbar">
        <a className="brand" href="/">
          知页<span>LEARNING ATLAS</span>
        </a>
        <div className="topbar-title">
          教材检查工作台<span className="badge">本地 · 只读</span>
        </div>
        <button onClick={refresh} disabled={refreshing}>
          {refreshing ? "正在刷新…" : "↻ 刷新数据"}
        </button>
      </header>
      {books.isPending && <div className="empty">正在读取教材…</div>}
      {error && (
        <div className="notice" role="alert">
          {error.message}{" "}
          <button onClick={() => setParams({})}>返回教材库</button>
        </div>
      )}
      {books.data?.length === 0 && (
        <div className="welcome">
          <span className="eyebrow">YOUR KNOWLEDGE, IN VIEW</span>
          <h1>从一本教材开始</h1>
          <p>导入教材后，在这里查看提取结果、知识之间的联系和原文依据。</p>
          <code>teach import "教材.pdf"</code>
          <p className="muted">导入完成后，点击右上角刷新。</p>
        </div>
      )}
      {!!books.data?.length && (
        <div className="workspace">
          <nav className="sidebar" aria-label="教材与章节">
            <div className="eyebrow">LIBRARY / 教材库</div>
            <label className="field">
              当前教材
              <select
                aria-label="当前教材"
                value={book}
                onChange={(e) => {
                  setParams({ book: e.target.value });
                }}
              >
                <option value="" disabled>
                  选择教材
                </option>
                {books.data.map((b) => (
                  <option key={b.id} value={b.id}>
                    {b.filename}
                  </option>
                ))}
              </select>
            </label>
            <label className="field">
              索引版本
              <select
                aria-label="索引版本"
                value={index}
                onChange={(e) => navigate({ index: e.target.value, point: "" })}
              >
                <option value="" disabled>
                  尚未提取
                </option>
                {catalog.data?.indexes.map((i) => (
                  <option key={i.id} value={i.id}>
                    {i.pipeline !== "semantic-v1" ? "历史旧版 · " : ""}
                    {i.status === "ready" ? "内容就绪" : "提取中"}
                    {i.indexed_sections?.length
                      ? `（${i.indexed_sections.length}/${catalog.data?.sections.length} 章）`
                      : ""}{" "}
                    · {new Date(i.created_at).toLocaleString("zh-CN")} ·{" "}
                    {i.id.slice(-6)}
                  </option>
                ))}
              </select>
            </label>
            <div className="nav-heading">
              章节目录 <span>{catalog.data?.sections.length ?? 0}</span>
            </div>
            {catalog.data?.sections.map((s, i) => (
              <button
                className={`chapter-link ${s.id === section ? "active" : ""}`}
                key={s.id}
                onClick={() => navigate({ section: s.id, point: "" })}
              >
                <span className="chapter-number">
                  {String(i + 1).padStart(2, "0")}
                </span>
                <span>
                  {s.title}
                  <small>
                    第 {s.start_page}–{s.end_page} 页
                  </small>
                </span>
              </button>
            ))}
            <div className="sidebar-note">
              内容来自本地已保存数据。
              <br />
              页面不会启动提取或修改教材。
            </div>
            {!!catalog.data?.book.unavailable_pages.length && (
              <p className="notice">
                不可读取页面：{catalog.data.book.unavailable_pages.join("、")}
              </p>
            )}
          </nav>
          <main className="main-content">
            <div className="eyebrow">CONTENT REVIEW / 内容检查</div>
            <h1>{chapter.data?.section.title ?? "选择章节"}</h1>
            <div className="chapter-meta">
              <span>
                PDF 第 {chapter.data?.section.start_page ?? "—"}–
                {chapter.data?.section.end_page ?? "—"} 页
              </span>
              <span className="badge">
                {chapter.data?.source === "snapshot"
                  ? "内容就绪"
                  : chapter.data?.source === "checkpoints"
                    ? "阶段结果 · 尚未完成"
                    : "仅原文"}
              </span>
            </div>
            {chapter.data && (
              <>
                <div className="stats">
                  <div>
                    <strong>{chapter.data.points.length}</strong>
                    <span>知识点</span>
                  </div>
                  <div>
                    <strong>{chapter.data.items.length}</strong>
                    <span>内容条目</span>
                  </div>
                  <div>
                    <strong>{chapter.data.saved_batches}</strong>
                    <span>已保存提取批次</span>
                  </div>
                </div>
                <section className="summary">
                  <span className="eyebrow">章节摘要</span>
                  <p>
                    {chapter.data.summary ??
                      (index
                        ? "章节摘要尚未就绪。可先检查已保存的知识点与原文。"
                        : "尚未创建知识索引，可先浏览原文。使用 teach index 提取后刷新。")}
                  </p>
                </section>
                {chapter.data.quality === "legacy" && (
                  <p className="notice">
                    旧版知识结构，仅供历史查看；新学习请重新提取。
                  </p>
                )}
                {chapter.data.index?.indexed_sections?.length && (
                  <p className="muted">
                    已审核章节：{chapter.data.index.indexed_sections.join("、")}
                    ；未列出的章节尚未索引。学习时仅分析当前及前序章节。
                  </p>
                )}
                {chapter.data.quality === "pending" && index && (
                  <p className="notice">
                    本章正在准备或审核，仅展示已完成章节审核的知识点。
                  </p>
                )}
                {!!chapter.data.exclusions?.length && (
                  <details className="coverage">
                    <summary>
                      未作为知识提取的内容（{chapter.data.exclusions.length}）
                    </summary>
                    {chapter.data.exclusions.map((item) => (
                      <div key={`${item.chunk_id}:${item.start}`}>
                        <p>{item.reason}</p>
                        <blockquote style={{ whiteSpace: "pre-wrap" }}>
                          {item.quote}
                        </blockquote>
                      </div>
                    ))}
                  </details>
                )}
                <details className="coverage">
                  <summary>提取状态与关系覆盖</summary>
                  <p>
                    最近记录阶段：{chapter.data.last_step ?? "暂无记录"}
                    （不表示任务仍在运行）
                  </p>
                  {chapter.data.last_error && (
                    <p className="notice">
                      最近历史失败：{chapter.data.last_error}
                    </p>
                  )}
                  {!chapter.data.coverage.length && (
                    <p>
                      {chapter.data.coverage_available
                        ? "尚无关系分析任务。"
                        : "旧数据库缺少关系检查记录，覆盖未知。"}
                    </p>
                  )}
                  {chapter.data.coverage.map((c) => (
                    <div key={c.task_id}>
                      <strong>
                        {c.section_id?.startsWith("@through:")
                          ? "截至目标章的候选"
                          : c.section_id?.startsWith("@rebuild:")
                            ? "章节重建候选"
                            : c.section_id
                              ? "当前章节"
                              : "全书候选"}{" "}
                        · {statuses[c.status] ?? c.status}
                      </strong>
                      <p>
                        已检查 {c.checked}/{c.total} 对 · 有关系{" "}
                        {c.with_relations} · 未建立关系 {c.without_relations} ·
                        未展开前置 {c.unexpanded}
                      </p>
                      {c.last_error && <p className="notice">{c.last_error}</p>}
                    </div>
                  ))}
                  <small>候选检查完成不代表潜在知识关系完备。</small>
                </details>
              </>
            )}
            <div className="tabs" role="tablist">
              <button
                role="tab"
                aria-selected={tab === "points"}
                onClick={() => setTab("points")}
              >
                知识点 {chapter.data?.points.length ?? 0}
              </button>
              <button
                role="tab"
                aria-selected={tab === "items"}
                onClick={() => setTab("items")}
              >
                内容清单 {chapter.data?.items.length ?? 0}
              </button>
            </div>
            <div className="filters">
              <input
                aria-label="搜索当前章节"
                placeholder="搜索当前章节的标题或描述…"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
              />
              <select
                aria-label="内容类型"
                value={kind}
                onChange={(e) => setKind(e.target.value)}
              >
                <option value="">全部类型</option>
                {Object.entries(kinds).map(([k, label]) => (
                  <option key={k} value={k}>
                    {label}
                  </option>
                ))}
              </select>
            </div>
            {chapter.isPending && <p className="muted">正在读取章节…</p>}
            {tab === "points"
              ? visiblePoints.map((p, i) => (
                  <article
                    key={p.key}
                    className={`point-card ${key === p.key ? "selected" : ""}`}
                  >
                    <div className="point-title">
                      <span className="point-number">
                        {String(i + 1).padStart(2, "0")}
                      </span>
                      <button className="text-button" onClick={() => select(p)}>
                        {p.title}
                      </button>
                    </div>
                    <p>{p.description}</p>
                    <div className="card-footer">
                      {refs(p)}
                      <button
                        className="text-button small"
                        onClick={() => select(p)}
                      >
                        查看关联知识 →
                      </button>
                    </div>
                    {key === p.key && point.data && (
                      <Relations
                        data={point.data}
                        sections={catalog.data?.sections ?? []}
                        select={select}
                        refs={refs}
                      />
                    )}
                    {key === p.key && point.isPending && (
                      <p>正在读取关联知识…</p>
                    )}
                  </article>
                ))
              : visibleItems.map((item, i) => (
                  <article className="point-card" key={i}>
                    <span className="badge neutral">
                      {kinds[item.kind] ?? item.kind}
                    </span>
                    <p>{item.description}</p>
                    <div className="item-points">
                      {item.point_keys.map((k) => {
                        const p = chapter.data?.points.find((p) => p.key === k);
                        return p ? (
                          <button key={k} onClick={() => select(p)}>
                            {p.title} ↗
                          </button>
                        ) : (
                          <span key={k}>缺失知识点：{k}</span>
                        );
                      })}
                    </div>
                    {refs(item)}
                  </article>
                ))}
            {!chapter.isPending &&
              (tab === "points"
                ? !visiblePoints.length
                : !visibleItems.length) && (
                <div className="empty compact">
                  {search || kind
                    ? "没有匹配内容，请调整筛选。"
                    : "此章节尚无已保存的提取结果。"}
                </div>
              )}
            {point.error && (
              <p className="notice" role="alert">
                {point.error.message}
              </p>
            )}
            <footer className="read-time">
              最近读取：
              {chapter.data
                ? new Date(chapter.data.read_at).toLocaleString("zh-CN")
                : "—"}{" "}
              · 所有操作仅查看
            </footer>
          </main>
          {book && chapter.data && (
            <Source
              key={`${book}-${section}`}
              book={book}
              initialPage={chapter.data.section.start_page}
              reference={reference}
              clear={() => setReference(null)}
            />
          )}
        </div>
      )}
    </div>
  );
}
