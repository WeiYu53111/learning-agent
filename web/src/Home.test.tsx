import { afterEach, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import App from "./App";
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});
function show() {
  return render(
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
}
it("shows tasks without loading any PDF or selecting a book", () => {
  const fetcher = vi.fn();
  vi.stubGlobal("fetch", fetcher);
  show();
  expect(screen.getByText("今天想做什么？")).toBeVisible();
  expect(screen.getByRole("link", { name: /导入教材/ })).toBeVisible();
  expect(screen.queryByText("原文对照")).toBeNull();
  expect(fetcher).not.toHaveBeenCalled();
});
it("selects a task before loading the library and supports search and home navigation", async () => {
  const fetcher = vi
    .fn()
    .mockResolvedValue({
      ok: true,
      json: async () => [
        { id: "b", filename: "教材.pdf", page_count: 10, section_count: 2 },
      ],
    });
  vi.stubGlobal("fetch", fetcher);
  show();
  fireEvent.click(screen.getByRole("link", { name: /浏览知识/ }));
  expect(await screen.findByText("教材.pdf")).toBeVisible();
  expect(fetcher).toHaveBeenCalledTimes(1);
  fireEvent.change(screen.getByPlaceholderText("输入文件名"), {
    target: { value: "missing" },
  });
  expect(screen.getByText(/没有匹配的教材/)).toBeVisible();
  fireEvent.click(screen.getByRole("link", { name: /返回首页/ }));
  expect(screen.getByText("今天想做什么？")).toBeVisible();
});
