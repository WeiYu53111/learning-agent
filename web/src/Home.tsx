import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useSearchParams } from "react-router-dom";
import ImportBook from "./ImportBook";
import { extractionRequest } from "./useExtraction";
import type { Book } from "./types";

export default function Home() {
  const [params] = useSearchParams();
  const view = params.get("view");
  const extracting = params.get("action") === "extraction";
  const [search, setSearch] = useState("");
  const [imported, setImported] = useState<Book | null>(null);
  const client = useQueryClient();
  const books = useQuery({
    queryKey: ["books"],
    queryFn: () => extractionRequest<Book[]>("/api/books"),
    enabled: view === "library",
  });
  const destination = (book: Book, extraction: boolean) =>
    `/?view=${extraction ? "extraction" : "browse"}&book=${encodeURIComponent(book.id)}`;
  return (
    <div className="app">
      <header className="topbar">
        <Link className="brand" to="/">
          知页<span>LEARNING ATLAS</span>
        </Link>
        <span className="badge">本地教材工作台</span>
        {view && <Link to="/">← 返回首页</Link>}
      </header>
      <main className="task-home">
        {!view || view === "home" ? (
          <>
            <span className="eyebrow">从这里开始</span>
            <h1>今天想做什么？</h1>
            <p className="muted">从教材到知识，选择你的下一步。</p>
            <div className="task-grid">
              {[
                [
                  "01",
                  "导入教材",
                  "上传 PDF、TXT 或 Markdown，加入本地教材库。",
                  "import",
                ],
                [
                  "02",
                  "提取知识",
                  "选择教材，开始或继续提取知识。",
                  "library&action=extraction",
                ],
                [
                  "03",
                  "浏览知识",
                  "查看知识点、知识关系和原文依据。",
                  "library&action=browse",
                ],
              ].map(([number, title, description, target]) => (
                <Link
                  className="task-card"
                  key={number}
                  to={`/?view=${target}`}
                >
                  <span className="task-number">{number}</span>
                  <h2>{title}</h2>
                  <p>{description}</p>
                  <span className="task-enter">开始 →</span>
                </Link>
              ))}
            </div>
          </>
        ) : view === "import" ? (
          <>
            <h1>导入教材</h1>
            <p className="muted">选择教材文件，导入后再决定下一步。</p>
            {imported ? (
              <section className="panel import-result">
                <h2>教材已就绪</h2>
                <p>{imported.filename}</p>
                <p className="muted">重复文件会直接使用已有教材。</p>
                <div className="actions">
                  <Link to={destination(imported, true)}>提取知识 →</Link>
                  <Link to={destination(imported, false)}>浏览教材 →</Link>
                  <Link to="/">返回首页</Link>
                </div>
                <button onClick={() => setImported(null)}>继续导入</button>
              </section>
            ) : (
              <ImportBook
                onImported={(book) => {
                  setImported(book);
                  void client.invalidateQueries({ queryKey: ["books"] });
                }}
              />
            )}
          </>
        ) : (
          <>
            <h1>{extracting ? "提取知识" : "浏览知识"}</h1>
            <p className="muted">先选择一本教材。</p>
            <label className="library-search">
              搜索教材
              <input
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                placeholder="输入文件名"
              />
            </label>
            {books.isPending && <p role="status">正在读取教材…</p>}
            {books.error && (
              <p role="alert">
                {books.error.message}{" "}
                <button onClick={() => void books.refetch()}>重试</button>
              </p>
            )}
            {books.data?.length === 0 && (
              <div className="empty">
                <p>还没有教材</p>
                <Link to="/?view=import">导入教材 →</Link>
              </div>
            )}
            {!!books.data?.length &&
              !books.data.some((b) =>
                b.filename.toLowerCase().includes(search.trim().toLowerCase()),
              ) && <p>没有匹配的教材，请换个关键词。</p>}
            <div className="book-grid">
              {books.data
                ?.filter((b) =>
                  b.filename
                    .toLowerCase()
                    .includes(search.trim().toLowerCase()),
                )
                .map((b) => (
                  <Link
                    className="task-card"
                    key={b.id}
                    to={destination(b, extracting)}
                  >
                    <h2>{b.filename}</h2>
                    <p>
                      {b.page_count != null
                        ? `${b.page_count} 页 · `
                        : "文本 · "}
                      {b.section_count} 章
                    </p>
                    <span className="task-enter">
                      {extracting ? "进入提取" : "开始浏览"} →
                    </span>
                  </Link>
                ))}
            </div>
          </>
        )}
      </main>
    </div>
  );
}
