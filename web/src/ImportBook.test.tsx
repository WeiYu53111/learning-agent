import { afterEach, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import ImportBook from "./ImportBook";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

it("uploads the selected PDF and selects the returned book", async () => {
  const book = { id: "book-1" };
  const fetcher = vi
    .fn()
    .mockResolvedValue({ ok: true, json: async () => book });
  vi.stubGlobal("fetch", fetcher);
  const onImported = vi.fn();
  render(<ImportBook onImported={onImported} />);
  expect(screen.getByRole("button")).toBeDisabled();
  const file = new File(["%PDF"], "教材.pdf", { type: "application/pdf" });
  fireEvent.change(screen.getByLabelText(/选择 PDF/), {
    target: { files: [file] },
  });
  fireEvent.click(screen.getByRole("button"));
  await waitFor(() => expect(onImported).toHaveBeenCalledWith(book));
  expect(fetcher).toHaveBeenCalledWith(
    expect.stringContaining("/api/import?filename="),
    expect.objectContaining({ method: "POST", body: file }),
  );
});

it("shows server errors and allows retry", async () => {
  vi.stubGlobal(
    "fetch",
    vi
      .fn()
      .mockResolvedValue({
        ok: false,
        json: async () => ({ detail: "文件不是有效 PDF。" }),
      }),
  );
  render(<ImportBook onImported={vi.fn()} />);
  fireEvent.change(screen.getByLabelText(/选择 PDF/), {
    target: { files: [new File(["bad"], "bad.pdf")] },
  });
  fireEvent.click(screen.getByRole("button"));
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "文件不是有效 PDF。",
  );
  expect(screen.getByRole("button")).toBeEnabled();
});
