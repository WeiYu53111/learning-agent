import { expect, test } from "@playwright/test";
import { readerPdf } from "./readerFixture";

test("PDF original renders selectable text, jumps pages and restores position", async ({
  page,
  request,
  context,
}) => {
  const response = await request.post("/api/import?filename=reader.pdf", {
    data: readerPdf(),
    headers: { "Content-Type": "application/pdf" },
  });
  expect(response.ok()).toBeTruthy();
  const book = await response.json();
  await page.goto("/");
  await page.getByRole("link", { name: /阅读原文件/ }).click();
  const opened = context.waitForEvent("page");
  await page.getByRole("link", { name: /reader.pdf/ }).click();
  const reader = await opened;
  await expect(reader.locator(".pdf-sheet canvas")).toBeVisible();
  await expect(reader.locator(".textLayer")).toContainText("Reader page 1");
  await reader.getByRole("button", { name: "下一页", exact: true }).click();
  await expect(reader.getByLabel("物理页码")).toHaveValue("2");
  await expect(reader.locator(".textLayer")).toContainText("Reader page 2");
  await reader.getByLabel("缩放").selectOption("1.5");
  await expect(reader.locator(".textLayer")).toContainText("Reader page 2");
  const selection = await reader.locator(".textLayer").evaluate((el) => {
    const range = document.createRange();
    range.selectNodeContents(el);
    window.getSelection()!.removeAllRanges();
    window.getSelection()!.addRange(range);
    return window.getSelection()!.toString();
  });
  expect(selection).toContain("Reader page 2");
  const collect = reader.getByRole("button", {
    name: "加入知识收件箱",
    exact: true,
  });
  await expect(collect).toBeEnabled();
  await collect.click();
  await expect(
    reader.getByRole("status").filter({ hasText: "已加入知识收件箱" }),
  ).toBeVisible();
  await reader.getByRole("button", { name: "关闭提示" }).click();
  await reader.route("**/api/personal/selections", (route) =>
    route.fulfill({
      status: 500,
      contentType: "application/json",
      body: JSON.stringify({ detail: "保存失败" }),
    }),
  );
  await reader.locator(".textLayer").evaluate((el) => {
    const range = document.createRange();
    range.selectNodeContents(el);
    window.getSelection()!.removeAllRanges();
    window.getSelection()!.addRange(range);
  });
  await expect(collect).toBeEnabled();
  await collect.click();
  await expect(reader.getByRole("alert")).toContainText("加入失败：保存失败");
  await reader.unroute("**/api/personal/selections");
  await reader.goto(`/?view=reader&book=${book.id}`);
  await expect(reader.getByLabel("物理页码")).toHaveValue("2");
  await reader.goto(`/?view=reader&book=${book.id}&page=1`);
  await expect(reader.locator(".textLayer")).toContainText("Reader page 1");
  await reader.getByRole("button", { name: "Chapter three" }).click();
  await expect(reader.getByLabel("物理页码")).toHaveValue("3");
  await reader.goto(`/?view=reader&book=${book.id}&page=999`);
  await expect(reader.getByLabel("物理页码")).toHaveValue("1");
  await expect(
    reader.getByText("位置超出范围，已回到文件开头。"),
  ).toBeVisible();
  await expect(reader.locator(".textLayer")).toContainText("Reader page 1");
  await reader.screenshot({
    path: "../output/reader-desktop.png",
    fullPage: true,
  });
  await reader.setViewportSize({ width: 390, height: 844 });
  await reader.getByLabel("缩放").selectOption("0");
  await expect(reader.locator(".textLayer")).toContainText("Reader page 1");
  await reader.screenshot({
    path: "../output/reader-mobile.png",
    fullPage: true,
  });
  expect(
    await reader.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBeTruthy();
});

test("text original is virtualized, inert and restores a targeted line", async ({
  page,
  request,
}) => {
  const text = Array.from(
    { length: 5000 },
    (_, i) => `line ${i + 1} <script>alert(1)</script>`,
  ).join("\r\n");
  const response = await request.post("/api/import?filename=reader.md", {
    data: text,
  });
  const book = await response.json();
  await page.goto(`/?view=reader&book=${book.id}&line=4000`);
  await expect(
    page.locator(".original-line").filter({ hasText: "line 4000 " }),
  ).toBeVisible();
  expect(await page.locator(".original-line").count()).toBeLessThan(100);
  await expect(page.locator(".original-line script")).toHaveCount(0);
  await page.waitForTimeout(1000);
  await page.reload();
  await expect(
    page.locator(".original-line").filter({ hasText: "line 4000 " }),
  ).toBeVisible();
  const download = await request.get(
    `/api/books/${book.id}/file?download=true`,
  );
  expect(await download.text()).toBe(text);
});
