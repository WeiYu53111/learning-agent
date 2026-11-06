import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";
export type Task = {
  id: string;
  status: string;
  index?: string;
  chapter?: string;
  block?: number;
  block_end?: number;
  total?: number;
  error?: string;
};
export const labels: Record<string, string> = {
  queued: "等待启动",
  running: "抽取中",
  stopping: "正在停止",
  stopped: "已停止",
  completed: "已完成",
  failed: "失败",
  interrupted: "异常中断",
};
export const activeTask = (task?: Task) =>
  !!task && ["queued", "running", "stopping"].includes(task.status);
export async function extractionRequest<T>(
  path: string,
  method = "GET",
): Promise<T> {
  const response = await fetch(path, method === "GET" ? undefined : { method });
  const body = await response.json();
  if (!response.ok) throw new Error(body.detail || "请求失败，请重试。");
  return body;
}
export function useExtraction(book: string) {
  const client = useQueryClient();
  const [, setParams] = useSearchParams();
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const path = `/api/books/${encodeURIComponent(book)}/extraction`;
  const query = useQuery<Task[]>({
    queryKey: ["extraction", book],
    queryFn: () => extractionRequest(path),
    enabled: !!book,
    refetchInterval: 2000,
  });
  function open(task: string) {
    setParams({ view: "extraction", book, task });
  }
  async function act(suffix: string) {
    setPending(true);
    setError("");
    try {
      const result = await extractionRequest<{ id: string }>(
        path + suffix,
        "POST",
      );
      await client.invalidateQueries({ queryKey: ["extraction", book] });
      await client.invalidateQueries({ queryKey: ["extraction-detail", book] });
      if (!suffix.endsWith("/stop")) open(result.id);
    } catch (e) {
      setError(e instanceof Error ? e.message : "操作失败");
    } finally {
      setPending(false);
    }
  }
  return { query, path, pending, error, act, open };
}
