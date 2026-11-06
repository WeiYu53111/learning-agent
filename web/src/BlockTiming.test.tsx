import { expect, it } from "vitest";
import { render, screen, cleanup } from "@testing-library/react";
import { BlockTiming, duration } from "./BlockTiming";
it("显示正在处理、完成及无历史计时，并正确格式化长耗时", () => {
  expect(duration(3661)).toBe("1 小时 1 分 1 秒");
  const timing = {
    started_at: "2026-11-06T00:00:00Z",
    finished_at: null,
    elapsed_seconds: 62,
    running: true,
  };
  const { rerender } = render(<BlockTiming timing={timing} />);
  expect(screen.getByText("已耗时 1 分 2 秒")).toBeVisible();
  rerender(<BlockTiming timing={{ ...timing, running: false }} />);
  expect(screen.getByText("用时 1 分 2 秒")).toBeVisible();
  rerender(<BlockTiming />);
  expect(screen.getByText("耗时未记录")).toBeVisible();
  cleanup();
});
