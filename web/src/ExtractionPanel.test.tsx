import { afterEach, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import ExtractionPanel from "./ExtractionPanel";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

it("停止立即反馈，并从当前块继续而不是重抽全章", async () => {
  let status = "running";
  const fetcher = vi.fn(async (url: string, init?: RequestInit) => {
    if (init?.method === "POST") {
      if (url.endsWith("/stop")) status = "stopped";
      return { ok: true, json: async () => ({ id: "task" }) };
    }
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
      <ExtractionPanel book="b" section="s1" />
    </QueryClientProvider>,
  );
  expect(await screen.findByText("栈")).toBeVisible();
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
