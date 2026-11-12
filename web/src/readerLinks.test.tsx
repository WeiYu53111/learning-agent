import { describe, expect, it } from "vitest";
import { readerUrl } from "./readerLinks";

describe("original file links", () => {
  it("preserves book identity and prioritizes physical page over fallback locations", () => {
    const params = new URL(
      readerUrl("book & 中文", { page: 8, start_line: 20 }, "c1", "s1"),
      "http://localhost",
    ).searchParams;
    expect(params.get("book")).toBe("book & 中文");
    expect(params.get("page")).toBe("8");
    expect(params.has("line")).toBe(false);
    expect(params.has("chunk")).toBe(false);
  });
  it("falls back from line to chunk to section without inventing a position", () => {
    expect(readerUrl("b", { start_line: 30 }, "c")).toContain("line=30");
    expect(readerUrl("b", undefined, "c", "s")).toContain("chunk=c");
    expect(readerUrl("b", undefined, undefined, "s")).toContain("section=s");
    expect(readerUrl("b")).toBe("/?view=reader&book=b");
  });
});
