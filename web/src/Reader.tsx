import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link, useSearchParams } from "react-router-dom";
import {
  getDocument,
  GlobalWorkerOptions,
  TextLayer,
  type PDFDocumentProxy,
} from "pdfjs-dist";
import worker from "pdfjs-dist/build/pdf.worker.min.mjs?url";
import "pdfjs-dist/web/pdf_viewer.css";
GlobalWorkerOptions.workerSrc = worker;
type Original = {
  id: string;
  filename: string;
  sha256: string;
  source_type: string;
  text?: string;
  sections: {
    id: string;
    title: string;
    start_page: number | null;
    chunk_ids: string[];
  }[];
};
async function read<T>(url: string): Promise<T> {
  const r = await fetch(url);
  const data = await r.json();
  if (!r.ok) throw new Error(data.detail || "原文件加载失败");
  return data;
}
export function initialPosition(
  explicit: string | null,
  stored: number,
  total: number,
) {
  const value = explicit === null ? stored : Number(explicit);
  return Number.isInteger(value) && value >= 1 && value <= total ? value : 1;
}

function PdfPage({
  pdf,
  page,
  zoom,
  width,
}: {
  pdf: PDFDocumentProxy;
  page: number;
  zoom: number;
  width: number;
}) {
  const host = useRef<HTMLDivElement>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    let dead = false,
      render:
        | ReturnType<Awaited<ReturnType<PDFDocumentProxy["getPage"]>>["render"]>
        | undefined,
      layer: TextLayer | undefined;
    const root = host.current!;
    root.replaceChildren();
    setLoading(true);
    setError("");
    void pdf
      .getPage(page)
      .then(async (p) => {
        if (dead) return;
        const base = p.getViewport({ scale: 1 });
        const scale = zoom || Math.max(0.1, (width - 24) / base.width);
        const viewport = p.getViewport({ scale });
        const canvas = document.createElement("canvas");
        const container = document.createElement("div");
        container.className = "textLayer";
        const ratio = Math.min(window.devicePixelRatio || 1, 2);
        canvas.width = Math.ceil(viewport.width * ratio);
        canvas.height = Math.ceil(viewport.height * ratio);
        canvas.style.width = `${viewport.width}px`;
        canvas.style.height = `${viewport.height}px`;
        root.style.width = `${viewport.width}px`;
        root.style.height = `${viewport.height}px`;
        root.style.setProperty("--scale-factor", String(scale));
        root.style.setProperty("--total-scale-factor", String(scale));
        root.append(canvas, container);
        render = p.render({
          canvas,
          viewport,
          transform: [ratio, 0, 0, ratio, 0, 0],
        });
        layer = new TextLayer({
          textContentSource: p.streamTextContent(),
          container,
          viewport,
        });
        await Promise.all([render.promise, layer.render()]);
        if (!dead) setLoading(false);
      })
      .catch((e) => {
        if (!dead) {
          setError(e.message);
          setLoading(false);
        }
      });
    return () => {
      dead = true;
      render?.cancel();
      layer?.cancel();
      root.replaceChildren();
    };
  }, [pdf, page, zoom, width, retry]);
  return (
    <>
      {loading && <p role="status">正在绘制页面…</p>}
      {error && (
        <p role="alert">
          页面无法显示：{error}{" "}
          <button onClick={() => setRetry((n) => n + 1)}>重试</button>
        </p>
      )}
      <div className="pdf-sheet" ref={host} />
    </>
  );
}

function TextPages({
  text,
  line,
  onLine,
}: {
  text: string;
  line: number;
  onLine: (line: number) => void;
}) {
  const lines = useMemo(() => text.split("\n"), [text]),
    host = useRef<HTMLDivElement>(null);
  const [top, setTop] = useState(0),
    [wrap, setWrap] = useState(true),
    [width, setWidth] = useState(600);
  useEffect(() => {
    const obs = new ResizeObserver((entries) =>
      setWidth(entries[0].contentRect.width),
    );
    obs.observe(host.current!);
    return () => obs.disconnect();
  }, []);
  const heights = useMemo(
    () =>
      lines.map((s) =>
        wrap
          ? Math.max(
              1,
              Math.ceil(
                Array.from(s).reduce(
                  (sum, c) =>
                    sum + (c.charCodeAt(0) > 255 ? 2 : c === "\t" ? 4 : 1),
                  0,
                ) / Math.max(1, Math.floor((width - 85) / 10)),
              ),
            ) * 25
          : 25,
      ),
    [lines, wrap, width],
  );
  const offsets = useMemo(() => {
    const values = [0];
    heights.forEach((h) => values.push(values[values.length - 1] + h));
    return values;
  }, [heights]);
  useEffect(() => {
    host.current?.scrollTo({ top: offsets[line - 1] || 0 });
  }, [line, wrap, width]);
  const first = Math.max(0, offsets.findIndex((y) => y >= top) - 5);
  let end = first;
  while (end < lines.length && offsets[end] < top + 1200) end++;
  return (
    <>
      <label>
        <input
          type="checkbox"
          checked={wrap}
          onChange={(e) => setWrap(e.target.checked)}
        />{" "}
        长行换行
      </label>
      <div
        className="text-reader-scroll"
        ref={host}
        onScroll={(e) => {
          const t = e.currentTarget.scrollTop;
          setTop(t);
          const current = Math.max(
            1,
            offsets.findIndex((y) => y > t),
          );
          onLine(current);
        }}
      >
        <div
          style={{ height: offsets[offsets.length - 1], position: "relative" }}
        >
          {lines.slice(first, end).map((s, i) => (
            <div
              className="original-line"
              key={first + i}
              style={{
                position: "absolute",
                top: offsets[first + i],
                minHeight: heights[first + i],
                width: wrap ? "100%" : undefined,
              }}
            >
              <span>{first + i + 1}</span>
              <code
                style={{
                  whiteSpace: wrap ? "pre-wrap" : "pre",
                  overflowWrap: "anywhere",
                }}
              >
                {s || " "}
              </code>
            </div>
          ))}
        </div>
      </div>
    </>
  );
}

export default function Reader() {
  const [params, setParams] = useSearchParams();
  const book = params.get("book") || "";
  const query = useQuery({
    queryKey: ["original", book],
    queryFn: () =>
      read<Original>(`/api/books/${encodeURIComponent(book)}/original`),
    enabled: !!book,
  });
  const [pdf, setPdf] = useState<PDFDocumentProxy | null>(null),
    [error, setError] = useState(""),
    [notice, setNotice] = useState(""),
    [position, setPosition] = useState(1),
    [zoom, setZoom] = useState(0),
    [width, setWidth] = useState(800),
    [outline, setOutline] = useState<{ title: string; page: number }[]>([]),
    [retry, setRetry] = useState(0),
    [ready, setReady] = useState(false);
  const surface = useRef<HTMLDivElement>(null),
    selectedLine = useRef(1);
  const data = query.data,
    isPdf = data?.source_type === "pdf";
  const storageKey = data ? `reader:${book}:${data.sha256}` : "";
  const total = isPdf
    ? pdf?.numPages || 1
    : data?.text?.split("\n").length || 1;
  useEffect(() => {
    if (!data || !isPdf) return;
    let dead = false;
    setError("");
    setPdf(null);
    setReady(false);
    const task = getDocument({
      url: `/api/books/${encodeURIComponent(book)}/file`,
      cMapUrl: "/assets/pdf/cmaps/",
      cMapPacked: true,
      standardFontDataUrl: "/assets/pdf/standard_fonts/",
      wasmUrl: "/assets/pdf/wasm/",
    });
    void task.promise
      .then(async (doc) => {
        if (dead) return;
        setPdf(doc);
        const items = await doc.getOutline();
        const list: { title: string; page: number }[] = [];
        async function visit(nodes: typeof items) {
          for (const item of nodes || []) {
            try {
              const dest =
                typeof item.dest === "string"
                  ? await doc.getDestination(item.dest)
                  : item.dest;
              if (dest)
                list.push({
                  title: item.title,
                  page:
                    (typeof dest[0] === "number"
                      ? dest[0]
                      : await doc.getPageIndex(dest[0])) + 1,
                });
            } catch {
              /* Invalid bookmarks do not block reading. */
            }
            await visit(item.items);
          }
        }
        await visit(items);
        if (!dead) setOutline(list);
      })
      .catch((e) => {
        if (!dead) setError(e.message);
      });
    return () => {
      dead = true;
      void task.destroy();
    };
  }, [data, book, isPdf, retry]);
  useEffect(() => {
    const el = surface.current;
    if (!el) return;
    const obs = new ResizeObserver((entries) =>
      setWidth(entries[0].contentRect.width),
    );
    obs.observe(el);
    return () => obs.disconnect();
  }, [data]);
  useEffect(() => {
    if (!data || (isPdf && !pdf)) return;
    let dead = false;
    setReady(false);
    void (async () => {
      let explicit = params.get(isPdf ? "page" : "line");
      let stored = 1;
      try {
        stored = Number(localStorage.getItem(storageKey)) || 1;
      } catch {}
      if (explicit === null && (params.has("chunk") || params.has("section"))) {
        const section = data.sections.find(
          (s) => s.id === params.get("section"),
        );
        if (isPdf && section?.start_page) explicit = String(section.start_page);
        else {
          const id = params.get("chunk") || section?.chunk_ids[0];
          if (id) {
            try {
              const c = await read<{
                page?: number;
                location?: { start_line?: number };
              }>(
                `/api/books/${encodeURIComponent(book)}/chunks/${encodeURIComponent(id)}`,
              );
              explicit = String(
                isPdf ? c.page || 1 : c.location?.start_line || 1,
              );
            } catch {
              explicit = "1";
            }
          }
        }
      }
      const target = initialPosition(explicit, stored, total);
      if (!dead) {
        setPosition(target);
        selectedLine.current = target;
        setNotice(
          explicit !== null && Number(explicit) !== target
            ? "位置超出范围，已回到文件开头。"
            : "",
        );
        setReady(true);
      }
    })();
    return () => {
      dead = true;
    };
  }, [data, pdf, params, book, isPdf, total, storageKey]);
  function remember(value: number) {
    try {
      localStorage.setItem(storageKey, String(value));
    } catch {}
  }
  useEffect(() => {
    if (ready) remember(position);
  }, [ready, position, storageKey]);
  function go(value: number) {
    const next = new URLSearchParams({
      view: "reader",
      book,
      [isPdf ? "page" : "line"]: String(value),
    });
    setParams(next, { replace: true });
  }
  useEffect(() => {
    if (isPdf || !ready) return;
    const save = () => {
      remember(selectedLine.current);
      const url = new URL(window.location.href);
      if (
        url.searchParams.get("view") !== "reader" ||
        url.searchParams.get("book") !== book
      )
        return;
      url.searchParams.set("line", String(selectedLine.current));
      window.history.replaceState(window.history.state, "", url);
    };
    const timer = window.setInterval(save, 800);
    window.addEventListener("pagehide", save);
    return () => {
      clearInterval(timer);
      window.removeEventListener("pagehide", save);
      remember(selectedLine.current);
    };
  }, [isPdf, ready, storageKey]);
  return (
    <div className="reader-app">
      <header className="topbar">
        <Link to="/">← 首页</Link>
        <h1>{data?.filename || "阅读原文件"}</h1>
        <Link to="/?view=library&action=reader">选择教材</Link>
        {data && (
          <a href={`/api/books/${encodeURIComponent(book)}/file?download=true`}>
            下载原文件
          </a>
        )}
      </header>
      {!book && <p>请先选择教材。</p>}
      {query.isLoading && <p role="status">正在读取原文件…</p>}
      {(query.error || error) && (
        <div role="alert">
          {query.error?.message || error}
          <button
            onClick={() => {
              void query.refetch();
              setRetry((n) => n + 1);
            }}
          >
            重试
          </button>
        </div>
      )}
      {data && (
        <>
          <div className="reader-toolbar">
            {isPdf && (
              <button
                disabled={!ready || position <= 1}
                onClick={() => go(position - 1)}
              >
                上一页
              </button>
            )}
            <label>
              {isPdf ? "物理页码" : "行号"}
              <input
                aria-label={isPdf ? "物理页码" : "行号"}
                key={`${position}:${ready}`}
                type="number"
                min={1}
                max={total}
                defaultValue={position}
                onKeyDown={(e) => {
                  if (e.key === "Enter") go(Number(e.currentTarget.value));
                }}
                onBlur={(e) => {
                  if (Number(e.target.value) !== position)
                    go(Number(e.target.value));
                }}
              />{" "}
              / {total}
            </label>
            {isPdf && (
              <>
                <button
                  disabled={!ready || position >= total}
                  onClick={() => go(position + 1)}
                >
                  下一页
                </button>
                <select
                  aria-label="缩放"
                  value={zoom}
                  onChange={(e) => setZoom(Number(e.target.value))}
                >
                  <option value={0}>适应宽度</option>
                  {[0.5, 0.75, 1, 1.25, 1.5, 2].map((z) => (
                    <option key={z} value={z}>
                      {z * 100}%
                    </option>
                  ))}
                </select>
              </>
            )}
            {notice && <span role="status">{notice}</span>}
          </div>
          <div className="reader-layout">
            <details className="reader-outline" open={window.innerWidth > 700}>
              <summary>目录</summary>
              {isPdf
                ? (outline.length
                    ? outline
                    : data.sections
                        .filter((s) => s.start_page)
                        .map((s) => ({ title: s.title, page: s.start_page! }))
                  ).map((o, i) => (
                    <button key={i} onClick={() => go(o.page)}>
                      {o.title}
                    </button>
                  ))
                : data.sections.map((s) => (
                    <button
                      key={s.id}
                      onClick={() =>
                        setParams({ view: "reader", book, section: s.id })
                      }
                    >
                      {s.title}
                    </button>
                  ))}
            </details>
            <main className="reader-surface" ref={surface}>
              {isPdf
                ? pdf &&
                  ready && (
                    <PdfPage
                      pdf={pdf}
                      page={position}
                      zoom={zoom}
                      width={width}
                    />
                  )
                : ready && (
                    <TextPages
                      text={data.text || ""}
                      line={position}
                      onLine={(n) => {
                        selectedLine.current = n;
                      }}
                    />
                  )}
            </main>
          </div>
        </>
      )}
    </div>
  );
}
