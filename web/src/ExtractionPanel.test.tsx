import { afterEach, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import ExtractionPage from "./ExtractionPage";
import { MemoryRouter } from "react-router-dom";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

it("停止立即反馈，并从当前块继续而不是重抽全章", async () => {
  let status = "running";
  const fetcher = vi.fn(async (url: string, init?: RequestInit) => {
    if (!url.includes("/extraction"))
      return { ok: true, json: async () => ({ sections: [] }) };
    if (init?.method === "POST") {
      if (url.endsWith("/stop")) status = "stopped";
      return { ok: true, json: async () => ({ id: "task" }) };
    }
    if (url.includes("/extraction/task"))
      return {
        ok: true,
        json: async () => ({
          task: { id: "task", status, chapter: "s1", block: 2, total: 30 },
          sections: [
            {
              id: "s1",
              title: "第一章",
              blocks: [{ number: 2, count: 1, quality: "pending" }],
            },
          ],
          selected: {
            chapter: "s1",
            block: 2,
            quality: "pending",
            items: [],
            chunks: [{ id: "c2", page: 2, text: "栈是后进先出结构" }],
            points: [
              {
                key: "p",
                title: "栈",
                description: "后进先出",
                source_refs: [{ chunk_id: "c2", quote: "后进先出" }],
              },
            ],
          },
          final: null,
          legacy: false,
        }),
      };
    return {
      ok: true,
      json: async () => [
        {
          id: "task",
          status,
          chapter: "s1",
          block: 2,
          total: 30,
          blocks: [
            {
              chapter: "s1",
              block: 2,
              quality: "pending",
              points: [
                {
                  key: "p",
                  title: "栈",
                  description: "后进先出",
                  source_refs: [],
                },
              ],
            },
          ],
        },
      ],
    };
  });
  vi.stubGlobal("fetch", fetcher);
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter
        initialEntries={["/?view=extraction&book=b&task=task&section=s9"]}
      >
        <ExtractionPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  expect(
    await screen.findByRole("button", { name: "栈 后进先出" }),
  ).toBeVisible();
  expect(screen.getByText("原文 · 第 2 页 · c2")).toBeVisible();
  expect(screen.getByRole("status")).toHaveTextContent("第 2 / 30 块");
  fireEvent.click(screen.getByRole("button", { name: "停止抽取" }));
  const resume = await screen.findByRole("button", { name: "从第 2 块继续" });
  await waitFor(() => expect(resume).toBeEnabled());
  fireEvent.click(resume);
  await waitFor(() =>
    expect(fetcher).toHaveBeenCalledWith(
      "/api/books/b/extraction?resume=task",
      { method: "POST" },
    ),
  );
});
