import { useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";
import Markdown from "react-markdown";
import { OriginalLink } from "./readerLinks";
import { cardLinks } from "./cardLinks";
import { Background, Controls, MarkerType, ReactFlow } from "@xyflow/react";
import "@xyflow/react/dist/style.css";

type Source = {
  book_id: string;
  filename: string;
  quote: string;
  chunk_id: string;
  index_id?: string;
  point_key?: string;
  available?: boolean;
  location: { page?: number; start_line?: number; end_line?: number };
};
type Card = {
  id: string;
  title: string;
  aliases: string[];
  body: string;
  topics: string[];
  archived: boolean;
  revision: number;
  sources: Source[];
  evidence: { session_id: string; needs_understanding: boolean }[];
};
type Candidate = {
  id: string;
  title: string;
  body: string;
  sources: Source[];
  evidence: Card["evidence"];
};
type Edge = {
  id: string;
  source: string;
  target: string;
  kind: string;
  reason?: string;
};
type Library = {
  cards: Card[];
  topics: { id: string; name: string }[];
  inbox: Candidate[];
  relations: Edge[];
  links: { source: string; target: string }[];
};
const labels: Record<string, string> = {
  contains: "包含",
  depends: "依赖",
  applies: "应用于",
  contrast: "对比",
  related: "相关",
  reference: "正文引用",
};
async function request<T>(
  path: string,
  method = "GET",
  body?: unknown,
): Promise<T> {
  const response = await fetch(`/api/personal${path}`, {
    method,
    headers: { "Content-Type": "application/json" },
    ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
  });
  const data = await response.json();
  if (!response.ok)
    throw new Error(
      typeof data.detail === "string" ? data.detail : "操作失败，请重试。",
    );
  return data;
}

function Sources({ sources }: { sources: Source[] }) {
  const [opened, setOpened] = useState<Source | null>(null);
  const original = useQuery<{ text: string }>({
    queryKey: ["personal-original", opened?.book_id, opened?.chunk_id],
    queryFn: async () => {
      const response = await fetch(
        `/api/books/${encodeURIComponent(opened!.book_id)}/chunks/${encodeURIComponent(opened!.chunk_id)}`,
      );
      if (!response.ok) throw new Error("原始来源不可用，以下摘录仍保留。");
      return response.json();
    },
    enabled: !!opened,
  });
  return (
    <div>
      {sources.map((s, i) => (
        <article className="personal-source" key={i}>
          <strong>{s.filename}</strong>
          <small>
            {s.location.page
              ? `第 ${s.location.page} 页`
              : `第 ${s.location.start_line ?? "—"}–${s.location.end_line ?? "—"} 行`}
          </small>
          <blockquote>{s.quote}</blockquote>
          {s.available === false ? (
            <p>原始来源不可用</p>
          ) : (
            <>
              <button onClick={() => setOpened(s)}>查看原文</button>
              <OriginalLink
                book={s.book_id}
                location={s.location}
                chunk={s.chunk_id}
              />
            </>
          )}
        </article>
      ))}
      {opened && (
        <div className="personal-source">
          <button onClick={() => setOpened(null)}>关闭原文</button>
          {original.error && <p role="alert">{original.error.message}</p>}
          <pre>{original.data?.text || opened.quote}</pre>
        </div>
      )}
    </div>
  );
}

function InboxItem({
  item,
  cards,
  act,
}: {
  item: Candidate;
  cards: Card[];
  act: (path: string, method: string, body?: unknown) => Promise<unknown>;
}) {
  const [title, setTitle] = useState(item.title),
    [body, setBody] = useState(item.body),
    [target, setTarget] = useState("");
  const similar = cards.filter((c) =>
    [c.title, ...c.aliases].some(
      (n) => n.toLowerCase() === title.toLowerCase(),
    ),
  );
  return (
    <article className="panel inbox-item">
      <label>
        候选标题
        <input value={title} onChange={(e) => setTitle(e.target.value)} />
      </label>
      <label>
        正文草稿
        <textarea value={body} onChange={(e) => setBody(e.target.value)} />
      </label>
      {item.evidence.some((e) => e.needs_understanding) && (
        <p className="notice">待理解：本轮学习仍有缺口。</p>
      )}
      {!!similar.length && (
        <p>
          可能已有：{similar.map((c) => c.title).join("、")}，可以选择补充。
        </p>
      )}
      <details>
        <summary>来源与学习记录（{item.sources.length} 个来源）</summary>
        <Sources sources={item.sources} />
        <p>{item.evidence.length} 条学习记录</p>
      </details>
      <div className="actions">
        <button
          disabled={!title.trim()}
          onClick={() =>
            void act(`/inbox/${item.id}/resolve`, "POST", {
              action: "accept",
              title,
              body,
            })
          }
        >
          新建卡片
        </button>
        <select
          aria-label="补充到卡片"
          value={target}
          onChange={(e) => setTarget(e.target.value)}
        >
          <option value="">选择已有卡片</option>
          {cards
            .filter((c) => !c.archived)
            .map((c) => (
              <option key={c.id} value={c.id}>
                {c.title} · {c.id.slice(-6)}
              </option>
            ))}
        </select>
        <button
          disabled={!target}
          onClick={() =>
            void act(`/inbox/${item.id}/resolve`, "POST", {
              action: "accept",
              target,
              revision: cards.find((c) => c.id === target)?.revision,
            })
          }
        >
          补充来源与记录
        </button>
        <button
          onClick={() =>
            void act(`/inbox/${item.id}/resolve`, "POST", { action: "ignore" })
          }
        >
          忽略
        </button>
      </div>
    </article>
  );
}

export default function Personal() {
  const [params, setParams] = useSearchParams();
  const client = useQueryClient();
  const query = useQuery({
    queryKey: ["personal"],
    queryFn: () => request<Library>(""),
  });
  const data = query.data;
  const suggestions = useQuery({
    queryKey: ["personal", "suggestions"],
    queryFn: () => request<Omit<Edge, "id">[]>("/suggestions"),
  });
  const [viewport, setViewport] = useState(window.innerWidth);
  useEffect(() => {
    const resize = () => setViewport(window.innerWidth);
    window.addEventListener("resize", resize);
    return () => window.removeEventListener("resize", resize);
  }, []);
  const selected = data?.cards.find((c) => c.id === params.get("card"));
  const [draft, setDraft] = useState<Card | null>(null),
    [dirty, setDirty] = useState(false),
    [error, setError] = useState(""),
    [busy, setBusy] = useState(false);
  const [search, setSearch] = useState(""),
    [topic, setTopic] = useState(""),
    [archived, setArchived] = useState(false),
    [preview, setPreview] = useState(false),
    [topicName, setTopicName] = useState("");
  const [target, setTarget] = useState(""),
    [kind, setKind] = useState("related"),
    [reason, setReason] = useState(""),
    [filter, setFilter] = useState("");
  const [linkSearch, setLinkSearch] = useState("");
  useEffect(() => {
    if (!dirty) setDraft(selected ? { ...selected } : null);
  }, [selected, dirty]);
  useEffect(() => {
    const warn = (e: BeforeUnloadEvent) => {
      if (dirty) {
        e.preventDefault();
        e.returnValue = "";
      }
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);
  // Restore the active URL on browser history navigation when an edit is retained.
  useEffect(() => {
    if (dirty && draft && params.get("card") !== draft.id) {
      if (window.confirm("正文尚未保存，放弃修改？")) setDirty(false);
      else setParams({ view: "personal", card: draft.id }, { replace: true });
    }
  }, [params, draft, dirty, setParams]);
  function navigate(values: Record<string, string>) {
    if (dirty && !window.confirm("正文尚未保存，放弃修改？")) return;
    setDirty(false);
    setParams(values);
  }
  async function act(path: string, method: string, body?: unknown) {
    setBusy(true);
    setError("");
    try {
      const result = await request(path, method, body);
      await client.invalidateQueries({ queryKey: ["personal"] });
      return result;
    } catch (e) {
      setError(e instanceof Error ? e.message : "操作失败");
      return null;
    } finally {
      setBusy(false);
    }
  }
  function edit(values: Partial<Card>) {
    setDraft(draft && { ...draft, ...values });
    setDirty(true);
  }
  async function save() {
    if (!draft) return;
    const saved = (await act(
      `/cards/${draft.id}`,
      "PUT",
      draft,
    )) as Card | null;
    if (saved) {
      setDirty(false);
      setDraft(saved);
    }
  }
  const edges: Edge[] = data
    ? [
        ...data.relations,
        ...data.links.map((e) => ({
          ...e,
          id: `link:${e.source}:${e.target}`,
          kind: "reference",
        })),
      ].filter((e) => draft && [e.source, e.target].includes(draft.id))
    : [];
  const shownEdges = edges.filter((e) => !filter || e.kind === filter);
  const neighborIds = new Set(shownEdges.flatMap((e) => [e.source, e.target]));
  if (draft) neighborIds.add(draft.id);
  const neighbors = data?.cards.filter((c) => neighborIds.has(c.id)) || [];
  return (
    <div className="app">
      <header className="topbar personal-topbar">
        <button onClick={() => navigate({})}>← 首页</button>
        <h1>个人知识库</h1>
        <button onClick={() => navigate({ view: "personal", tab: "inbox" })}>
          收件箱 {data?.inbox.length ?? 0}
        </button>
        <button onClick={() => navigate({ view: "personal" })}>全部卡片</button>
      </header>
      {error && (
        <p className="notice" role="alert">
          {error}
        </p>
      )}
      {query.error && (
        <p role="alert">
          {query.error.message}
          <button onClick={() => void query.refetch()}>重试</button>
        </p>
      )}
      {query.isPending && <p role="status">正在读取知识库…</p>}
      <fieldset disabled={busy} className="personal-fieldset">
        {params.get("tab") === "inbox" ? (
          <main className="task-home">
            <h2>等待你整理</h2>
            <p>确认后加入个人知识库；补充已有卡片不会改写正文。</p>
            {data?.inbox.length === 0 && (
              <p className="empty">
                收件箱为空。完成学习或在知识点上点击“加入收件箱”。
              </p>
            )}
            {data?.inbox.map((item) => (
              <InboxItem
                key={item.id}
                item={item}
                cards={data.cards}
                act={act}
              />
            ))}
          </main>
        ) : (
          <div className="personal-layout">
            <nav className="panel personal-list">
              <button
                onClick={async () => {
                  if (dirty && !window.confirm("放弃未保存修改？")) return;
                  const c = (await act("/cards", "POST", {
                    title: "未命名卡片",
                  })) as Card | null;
                  if (c) {
                    setDirty(false);
                    setParams({ view: "personal", card: c.id });
                  }
                }}
              >
                ＋ 新建卡片
              </button>
              <input
                aria-label="搜索卡片"
                placeholder="搜索标题、别名、正文"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
              />
              <select
                aria-label="筛选主题"
                value={topic}
                onChange={(e) => setTopic(e.target.value)}
              >
                <option value="">全部主题</option>
                {data?.topics.map((t) => (
                  <option key={t.id} value={t.id}>
                    {t.name}
                  </option>
                ))}
              </select>
              <label>
                <input
                  type="checkbox"
                  checked={archived}
                  onChange={(e) => setArchived(e.target.checked)}
                />{" "}
                查看归档
              </label>
              {data?.cards
                .filter(
                  (c) =>
                    c.archived === archived &&
                    (!topic || c.topics.includes(topic)) &&
                    `${c.title} ${c.aliases.join(" ")} ${c.body}`
                      .toLowerCase()
                      .includes(search.toLowerCase()),
                )
                .map((c) => (
                  <button
                    key={c.id}
                    aria-current={c.id === draft?.id ? "page" : undefined}
                    onClick={() => navigate({ view: "personal", card: c.id })}
                  >
                    {c.title}
                  </button>
                ))}
              <details>
                <summary>管理主题</summary>
                <input
                  aria-label="主题名称"
                  value={topicName}
                  onChange={(e) => setTopicName(e.target.value)}
                />
                <button
                  disabled={!topicName.trim()}
                  onClick={() =>
                    void act("/topics", "POST", { name: topicName })
                  }
                >
                  新建主题
                </button>
                {data?.topics.map((t) => (
                  <div key={t.id}>
                    <span>{t.name}</span>
                    <button
                      onClick={() => {
                        const name = window.prompt("主题新名称", t.name);
                        if (name) void act(`/topics/${t.id}`, "PUT", { name });
                      }}
                    >
                      改名
                    </button>
                    <button
                      onClick={() => void act(`/topics/${t.id}`, "DELETE")}
                    >
                      删除主题
                    </button>
                  </div>
                ))}
              </details>
            </nav>
            {draft ? (
              <>
                <main className="panel personal-editor">
                  <label>
                    标题
                    <input
                      value={draft.title}
                      onChange={(e) => edit({ title: e.target.value })}
                    />
                  </label>
                  <label>
                    别名（逗号分隔）
                    <input
                      value={draft.aliases.join(",")}
                      onChange={(e) =>
                        edit({ aliases: e.target.value.split(",") })
                      }
                    />
                  </label>
                  <div className="actions">
                    <button disabled={!dirty} onClick={() => void save()}>
                      保存
                    </button>
                    <span role="status">
                      {dirty ? "有未保存修改" : "已保存"}
                    </span>
                    <button onClick={() => setPreview(!preview)}>
                      {preview ? "编辑正文" : "预览 Markdown"}
                    </button>
                    <button onClick={() => edit({ archived: !draft.archived })}>
                      {draft.archived
                        ? "恢复卡片（保存生效）"
                        : "归档卡片（保存生效）"}
                    </button>
                  </div>
                  {preview ? (
                    <div className="markdown-preview">
                      <Markdown
                        remarkPlugins={[cardLinks]}
                        skipHtml
                        components={{
                          a: ({ href, children }) =>
                            href?.startsWith("?view=personal&card=") ? (
                              <button
                                onClick={() =>
                                  navigate({
                                    view: "personal",
                                    card: href.split("card=")[1],
                                  })
                                }
                              >
                                {children}
                              </button>
                            ) : (
                              <a href={href} target="_blank" rel="noreferrer">
                                {children}
                              </a>
                            ),
                        }}
                      >
                        {draft.body}
                      </Markdown>
                    </div>
                  ) : (
                    <textarea
                      className="card-body"
                      aria-label="Markdown 正文"
                      value={draft.body}
                      onChange={(e) => edit({ body: e.target.value })}
                    />
                  )}
                  {!preview && (
                    <details open={draft.body.endsWith("[[")}>
                      <summary>插入卡片链接</summary>
                      <input
                        aria-label="搜索链接目标"
                        value={linkSearch}
                        onChange={(e) => setLinkSearch(e.target.value)}
                      />
                      {data?.cards
                        .filter(
                          (c) =>
                            c.id !== draft.id && c.title.includes(linkSearch),
                        )
                        .map((c) => (
                          <button
                            key={c.id}
                            onClick={() =>
                              edit({
                                body:
                                  draft.body.replace(/\[\[$/, "") +
                                  `[[${c.id}|${c.title}]]`,
                              })
                            }
                          >
                            {c.title}
                            {c.archived ? "（已归档）" : ""} · {c.id.slice(-6)}
                          </button>
                        ))}
                    </details>
                  )}
                  <h2>局部图谱</h2>
                  <select
                    aria-label="筛选关系"
                    value={filter}
                    onChange={(e) => setFilter(e.target.value)}
                  >
                    <option value="">全部关系</option>
                    {Object.entries(labels).map(([k, v]) => (
                      <option key={k} value={k}>
                        {v}
                      </option>
                    ))}
                  </select>
                  <div className="personal-graph">
                    <ReactFlow
                      fitView
                      key={`${draft.id}:${filter}:${neighbors.length}:${viewport}`}
                      nodes={neighbors.map((c, i) => ({
                        id: c.id,
                        data: {
                          label: c.title + (c.archived ? "（已归档）" : ""),
                        },
                        position:
                          c.id === draft.id
                            ? { x: 240, y: 130 }
                            : {
                                x:
                                  240 +
                                  200 *
                                    Math.cos(
                                      (i * 2 * Math.PI) / neighbors.length,
                                    ),
                                y:
                                  130 +
                                  100 *
                                    Math.sin(
                                      (i * 2 * Math.PI) / neighbors.length,
                                    ),
                              },
                      }))}
                      edges={shownEdges.map((e) => ({
                        ...e,
                        label: labels[e.kind],
                        ...(["contrast", "related"].includes(e.kind)
                          ? {}
                          : { markerEnd: { type: MarkerType.ArrowClosed } }),
                        style: {
                          strokeDasharray:
                            e.kind === "reference" ? "5 5" : undefined,
                        },
                      }))}
                      onNodeClick={(_, n) =>
                        navigate({ view: "personal", card: n.id })
                      }
                    >
                      <Background />
                      <Controls />
                    </ReactFlow>
                  </div>
                </main>
                <aside className="panel personal-details">
                  <h2>主题</h2>
                  {data?.topics.map((t) => (
                    <label key={t.id}>
                      <input
                        type="checkbox"
                        checked={draft.topics.includes(t.id)}
                        onChange={(e) =>
                          edit({
                            topics: e.target.checked
                              ? [...draft.topics, t.id]
                              : draft.topics.filter((id) => id !== t.id),
                          })
                        }
                      />
                      {t.name}
                    </label>
                  ))}
                  <h2>来源</h2>
                  {!draft.sources.length && (
                    <p>个人想法，尚无来源。可以从收件箱补充。</p>
                  )}
                  <Sources sources={draft.sources} />
                  <h2>关系与反向链接</h2>
                  {suggestions.data
                    ?.filter(
                      (e) => e.source === draft.id || e.target === draft.id,
                    )
                    .map((e, i) => (
                      <div className="personal-source" key={i}>
                        <small>待确认的教材关系</small>
                        <p>
                          {data?.cards.find((c) => c.id === e.source)?.title} →{" "}
                          {labels[e.kind]} →{" "}
                          {data?.cards.find((c) => c.id === e.target)?.title}
                        </p>
                        <p>{e.reason}</p>
                        <button
                          onClick={() => void act("/relations", "POST", e)}
                        >
                          确认关系
                        </button>
                      </div>
                    ))}
                  {edges.map((e) => (
                    <div key={e.id}>
                      <button
                        onClick={() =>
                          navigate({
                            view: "personal",
                            card: e.source === draft.id ? e.target : e.source,
                          })
                        }
                      >
                        {data?.cards.find((c) => c.id === e.source)?.title} →{" "}
                        {labels[e.kind]} →{" "}
                        {data?.cards.find((c) => c.id === e.target)?.title}
                      </button>
                      <p>{e.reason}</p>
                      {e.kind !== "reference" && (
                        <button
                          onClick={() =>
                            void act(`/relations/${e.id}`, "DELETE")
                          }
                        >
                          移除关系
                        </button>
                      )}
                    </div>
                  ))}
                  <select
                    aria-label="关系目标"
                    value={target}
                    onChange={(e) => setTarget(e.target.value)}
                  >
                    <option value="">选择卡片</option>
                    {data?.cards
                      .filter((c) => c.id !== draft.id)
                      .map((c) => (
                        <option key={c.id} value={c.id}>
                          {c.title} · {c.id.slice(-6)}
                        </option>
                      ))}
                  </select>
                  <select
                    aria-label="关系类型"
                    value={kind}
                    onChange={(e) => setKind(e.target.value)}
                  >
                    {Object.entries(labels)
                      .filter(([k]) => k !== "reference")
                      .map(([k, v]) => (
                        <option key={k} value={k}>
                          {v}
                        </option>
                      ))}
                  </select>
                  <input
                    placeholder="关系说明"
                    value={reason}
                    onChange={(e) => setReason(e.target.value)}
                  />
                  <button
                    disabled={!target}
                    onClick={() =>
                      void act("/relations", "POST", {
                        source: draft.id,
                        target,
                        kind,
                        reason,
                      })
                    }
                  >
                    建立关系
                  </button>
                  <details>
                    <summary>学习记录（{draft.evidence.length}）</summary>
                    {draft.evidence.map((e, i) => (
                      <p key={i}>
                        {e.session_id} ·{" "}
                        {e.needs_understanding ? "待理解" : "本轮达标"}
                      </p>
                    ))}
                  </details>
                </aside>
              </>
            ) : (
              <main className="panel empty">
                选择一张卡片，或新建自己的想法。
              </main>
            )}
          </div>
        )}
      </fieldset>
    </div>
  );
}
