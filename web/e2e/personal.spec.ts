import { test, expect } from "@playwright/test";

test("collect, organize and preserve a personal card on desktop and mobile", async ({
  page,
  request,
}) => {
  await page.goto("/");
  await page.getByRole("link", { name: /个人知识库/ }).click();
  await expect(
    page.getByText("选择一张卡片，或新建自己的想法。"),
  ).toBeVisible();
  const books = await (await request.get("/api/books")).json();
  const catalog = await (await request.get(`/api/books/${books[0].id}`)).json();
  const index = catalog.indexes.find(
    (i: { id: string }) => i.id !== "older_index",
  );
  const chapter = await (
    await request.get(`/api/books/${books[0].id}/sections/s1?index=${index.id}`)
  ).json();
  const collected = await request.post("/api/personal/collect", {
    data: { book: books[0].id, index: index.id, key: chapter.points[0].key },
  });
  expect(collected.ok()).toBeTruthy();
  await page.reload();
  await page.getByRole("button", { name: /收件箱/ }).click();
  await page.getByLabel("候选标题").fill("我的上下文理解");
  await page.getByRole("button", { name: "新建卡片", exact: true }).click();
  await expect(page.getByText(/收件箱为空/)).toBeVisible();
  await page.getByRole("button", { name: "全部卡片" }).click();
  await page
    .getByRole("button", { name: "我的上下文理解", exact: true })
    .click();
  await page
    .getByLabel("Markdown 正文")
    .fill(
      "# 我的理解\n\n这是**自己的话**。\n\n<script>window.injected = true</script>",
    );
  await page.getByRole("button", { name: "保存", exact: true }).click();
  await expect(page.getByRole("status")).toHaveText("已保存");
  await page.getByRole("button", { name: "预览 Markdown" }).click();
  await expect(page.getByRole("heading", { name: "我的理解" })).toBeVisible();
  await expect(page.locator(".markdown-preview script")).toHaveCount(0);
  await page.screenshot({
    path: "../output/personal-desktop.png",
    fullPage: true,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({
    path: "../output/personal-mobile.png",
    fullPage: true,
  });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBeTruthy();
  await page.getByRole("button", { name: "编辑正文" }).click();
  await page.getByLabel("Markdown 正文").fill("未保存内容");
  page.once("dialog", (dialog) => dialog.dismiss());
  await page.getByRole("button", { name: "全部卡片" }).click();
  await expect(page.getByLabel("Markdown 正文")).toHaveValue("未保存内容");
});
