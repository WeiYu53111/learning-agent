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
import Markdown from "react-markdown";
import Personal from "./Personal";
import { cardLinks } from "./cardLinks";
vi.mock("@xyflow/react", () => ({
  ReactFlow: () => <div>图谱</div>,
  Background: () => null,
  Controls: () => null,
  MarkerType: { ArrowClosed: "arrow" },
}));
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});
it("renders card references but leaves code literal and suppresses HTML", () => {
  const { container } = render(
    <Markdown skipHtml remarkPlugins={[cardLinks]}>
      {"[[card_abc|目标]]\n\n`[[card_def|代码]]`\n\n<script>alert(1)</script>"}
    </Markdown>,
  );
  expect(screen.getByRole("link", { name: "目标" })).toHaveAttribute(
    "href",
    "?view=personal&card=card_abc",
  );
  expect(screen.queryByRole("link", { name: "代码" })).toBeNull();
  expect(container.querySelector("script")).toBeNull();
});
it("preserves edits when revision conflict is returned", async () => {
  const card = {
    id: "card_a",
    title: "已有卡片",
    aliases: [],
    body: "原文",
    topics: [],
    archived: false,
    revision: 1,
    sources: [],
    evidence: [],
  };
  const fetcher = vi.fn(async (url: string, options: RequestInit) => {
    if (options.method === "PUT")
      return {
        ok: false,
        json: async () => ({ detail: "卡片已在其他窗口修改" }),
      };
    return {
      ok: true,
      json: async () =>
        url.endsWith("suggestions")
          ? []
          : { cards: [card], topics: [], inbox: [], relations: [], links: [] },
    };
  });
  vi.stubGlobal("fetch", fetcher);
  render(
    <QueryClientProvider
      client={
        new QueryClient({ defaultOptions: { queries: { retry: false } } })
      }
    >
      <MemoryRouter initialEntries={["/?view=personal&card=card_a"]}>
        <Personal />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  fireEvent.change(await screen.findByLabelText("Markdown 正文"), {
    target: { value: "新的个人理解" },
  });
  fireEvent.click(screen.getByRole("button", { name: /^保存$/ }));
  await waitFor(() =>
    expect(screen.getByRole("alert")).toHaveTextContent("其他窗口"),
  );
  expect(screen.getByLabelText("Markdown 正文")).toHaveValue("新的个人理解");
});
