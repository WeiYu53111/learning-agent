import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import App, { Highlight } from "./App";
import { graphData, relationGroup } from "./Relations";
import type { PointView } from "./types";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});
const ref = { chunk_id: "c1", quote: "上下文是缓存的基础。" };
const point = {
  key: "p1",
  section_id: "s1",
  title: "上下文",
  description: "上下文是缓存的基础。",
  source_refs: [ref],
};
const next = { ...point, key: "p2", section_id: "s2", title: "缓存" };

describe("原文与关系", () => {
  it("只高亮唯一逐字引用，HTML 始终显示为文本", () => {
    const { container, rerender } = render(
      <Highlight
        text={"原文<script>alert(1)</script>尾部"}
        quote={"<script>alert(1)</script>"}
      />,
    );
    expect(container.querySelector("mark")).toHaveTextContent(
      "<script>alert(1)</script>",
    );
    expect(container.querySelector("script")).toBeNull();
    rerender(<Highlight text="重复 重复" quote="重复" />);
    expect(container.querySelector("mark")).toBeNull();
    expect(screen.getByText("引用无法唯一匹配此文本块。")).toBeVisible();
  });
  it("保留先修方向、相关关系并合并重复节点", () => {
    const prerequisite = {
      source: "p1",
      target: "p2",
      kind: "prerequisite" as const,
      reason: "前置",
    };
    expect(relationGroup(prerequisite, "p1")).toBe("后续依赖");
    expect(relationGroup(prerequisite, "p2")).toBe("前置知识");
    const data: PointView = {
      point,
      neighbors: [next],
      pairs: [],
      missing_keys: [],
      coverage_available: true,
      relations: [prerequisite, { ...prerequisite, kind: "related" }],
    };
    const graph = graphData(data);
    expect(graph.nodes).toHaveLength(2);
    expect(graph.edges).toHaveLength(2);
    expect(graph.edges[0].markerEnd).toBeTruthy();
    expect(graph.edges[1].markerEnd).toBeUndefined();
    expect(graph.nodes[1].data.label).toContain("后续依赖 / 相关知识");
  });
});

it("浏览已保存章节、筛选内容、点击引用并保留物理页码", async () => {
  Element.prototype.scrollIntoView = vi.fn();
  const book = {
    id: "b",
    filename: "教材.pdf",
    page_count: 2,
    section_count: 1,
    unavailable_pages: [],
  };
  const section = {
    id: "s1",
    title: "上下文与缓存",
    start_page: 1,
    end_page: 2,
  };
  const index = {
    id: "i",
    pipeline: "semantic-v1",
    status: "ready",
    created_at: "2026-10-26T00:00:00Z",
  };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      let data: unknown;
      if (url === "/api/books") data = [book];
      else if (url === "/api/books/b")
        data = { book, sections: [section], indexes: [index] };
      else if (url.includes("/sections/"))
        data = {
          section,
          index,
          source: "snapshot",
          points: [point],
          items: [
            {
              kind: "mechanism",
              description: "缓存机制",
              point_keys: ["p1"],
              source_refs: [ref],
            },
          ],
          summary: "章节摘要内容",
          saved_batches: 1,
          coverage: [],
          coverage_available: true,
          read_at: index.created_at,
        };
      else if (url.includes("/chunks/"))
        data = { id: "c1", page: 2, text: ref.quote };
      else
        data = {
          page: url.endsWith("/2") ? 2 : 1,
          page_count: 2,
          chunks: [{ id: "c1", page: 2, text: ref.quote }],
          unavailable: false,
        };
      return { ok: true, json: async () => data };
    }),
  );
  render(
    <QueryClientProvider
      client={
        new QueryClient({ defaultOptions: { queries: { retry: false } } })
      }
    >
      <MemoryRouter>
        <App />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  expect(
    await screen.findByRole("heading", { name: "上下文与缓存" }),
  ).toBeVisible();
  fireEvent.change(screen.getByLabelText("搜索当前章节"), {
    target: { value: "找不到" },
  });
  expect(screen.getByText("没有匹配内容，请调整筛选。")).toBeVisible();
  fireEvent.change(screen.getByLabelText("搜索当前章节"), {
    target: { value: "" },
  });
  fireEvent.click(screen.getByRole("button", { name: "↗ 原文 1" }));
  expect(await screen.findByText("选中引用")).toBeVisible();
  expect(await screen.findByDisplayValue("2")).toBeVisible();
  fireEvent.click(screen.getByRole("tab", { name: "内容清单 1" }));
  expect(screen.getByText("缓存机制")).toBeVisible();
});
