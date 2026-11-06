import { afterEach, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import ExtractionPage from "./ExtractionPage";
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

it("已排除文本块显示明确原因而非正在处理提示", async () => {
  const task = {
    id: "t",
    status: "stopped",
    chapter: "s1",
    block: 1,
    total: 2,
  };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => ({
      ok: true,
      json: async () =>
        url.endsWith("/extraction")
          ? [task]
          : {
              task,
              sections: [
                {
                  id: "s1",
                  title: "前言与目录",
                  blocks: [
                    {
                      number: 1,
                      count: 0,
                      quality: "excluded",
                      excluded_roles: ["navigation"],
                    },
                  ],
                },
              ],
              selected: {
                chapter: "s1",
                block: 1,
                quality: "excluded",
                points: [],
                items: [],
                chunks: [{ id: "c1", page: 1, text: "目录条目" }],
                exclusions: [
                  {
                    role: "navigation",
                    reason: "只有目录条目和页码，没有正文。",
                    chunk_id: "c1",
                    start: 0,
                    end: 4,
                  },
                ],
              },
              final: null,
              legacy: false,
            },
    })),
  );
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/?view=extraction&book=b&task=t"]}>
        <ExtractionPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  expect(
    await screen.findByText("只有目录条目和页码，没有正文。"),
  ).toBeVisible();
  expect(
    screen.getByRole("button", { name: /第 1 块 · 已排除：目录导航/ }),
  ).toBeVisible();
  expect(screen.queryByText(/当前块正在处理/)).not.toBeInTheDocument();
});
it("历史块选择在刷新和完成后保持，完成后可切换整合结果", async () => {
  let status = "running";
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const content = (title: string) => ({
    points: [{ key: title, title, description: "说明", source_refs: [] }],
    items: [],
    chunks: [],
  });
  const fetcher = vi.fn(async (url: string) => {
    const task = { id: "t", status, chapter: "s2", block: 2, total: 2 };
    const block = url.includes("block=1") ? 1 : 2;
    return {
      ok: true,
      json: async () =>
        url.endsWith("/extraction")
          ? [task]
          : {
              task,
              sections: [
                {
                  id: "s2",
                  title: "第二章",
                  blocks: [1, 2].map((number) => ({
                    number,
                    count: 1,
                    quality: "block_reviewed",
                  })),
                },
              ],
              selected: {
                ...content(`知识${block}`),
                chapter: "s2",
                block,
                quality: "block_reviewed",
              },
              final: status === "completed" ? content("整合知识") : null,
              legacy: false,
            },
    };
  });
  vi.stubGlobal("fetch", fetcher);
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/?view=extraction&book=b&task=t"]}>
        <ExtractionPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  expect(
    await screen.findByRole("button", { name: "知识2 说明" }),
  ).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: /第 1 块 · 1 个知识点/ }));
  expect(
    await screen.findByRole("button", { name: "知识1 说明" }),
  ).toBeVisible();
  status = "completed";
  await client.invalidateQueries({ queryKey: ["extraction-detail"] });
  await waitFor(() =>
    expect(screen.getByRole("status")).toHaveTextContent("已完成"),
  );
  expect(screen.getByRole("button", { name: "知识1 说明" })).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "章节整合结果" }));
  expect(screen.getByRole("button", { name: "整合知识 说明" })).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "跟随当前抽取" }));
  expect(
    await screen.findByRole("button", { name: "知识2 说明" }),
  ).toBeVisible();
});
