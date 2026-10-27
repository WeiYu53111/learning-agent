import { expect, test } from "@playwright/test";

test("章节、引用、跨章关联、图节点、刷新及版本隔离", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto("/");
  await expect(
    page.getByRole("heading", { name: "理解上下文", exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "查看关联知识 →" }).click();
  await expect(
    page.getByRole("heading", { name: "后续依赖", exact: true }),
  ).toBeVisible();
  await expect(page.getByLabel("局部知识关系图")).toBeVisible();
  for (const handle of await page.locator(".react-flow__handle").all()) {
    const bounds = await handle.boundingBox();
    expect(bounds).not.toBeNull();
    expect(bounds!.width).toBeLessThan(20);
    expect(bounds!.height).toBeLessThan(20);
  }
  await page
    .getByRole("button", { name: "↗ 原文 1", exact: true })
    .first()
    .click();
  await expect(page.locator("mark")).toContainText("上下文包含用户消息");
  await page.screenshot({
    path: "test-results/inspection.png",
    fullPage: true,
  });
  await page
    .getByRole("button", { name: "缓存与消息前缀 ↗", exact: true })
    .first()
    .click();
  await expect(
    page.getByRole("heading", { name: "缓存与消息前缀", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "前置知识", exact: true }),
  ).toBeVisible();
  await page.reload();
  await expect(
    page.getByRole("heading", { name: "前置知识", exact: true }),
  ).toBeVisible();
  await page
    .locator(".react-flow__node")
    .filter({ hasText: "上下文的组成" })
    .click();
  await expect(
    page.getByRole("heading", { name: "理解上下文", exact: true }),
  ).toBeVisible();
  await page.goBack();
  await expect(
    page.getByRole("heading", { name: "缓存与消息前缀", exact: true }),
  ).toBeVisible();
  await page.getByLabel("索引版本").selectOption("older_index");
  await page.getByRole("button", { name: "查看关联知识 →" }).click();
  await expect(
    page.getByText("此知识点暂无已保存关系。请结合下方候选检查情况判断。"),
  ).toBeVisible();
  await page.getByLabel("搜索当前章节").fill("无匹配");
  await expect(page.getByText("没有匹配内容，请调整筛选。")).toBeVisible();
  await page.getByLabel("搜索当前章节").fill("");
  await page.getByLabel("原文页码").fill("3");
  await expect(page.getByText("此页不可读取，当前不支持 OCR。")).toBeVisible();
  expect(errors).toEqual([]);
});
