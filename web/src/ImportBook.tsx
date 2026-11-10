import { useState } from "react";
import type { Book } from "./types";

export default function ImportBook({
  onImported,
}: {
  onImported: (book: Book) => void;
}) {
  const [file, setFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function upload() {
    if (!file || busy) return;
    setError("");
    if (file.size > 20 * 1024 * 1024) {
      setError("文件不能超过 20 MiB。");
      return;
    }
    setBusy(true);
    try {
      const response = await fetch(
        `/api/import?filename=${encodeURIComponent(file.name)}`,
        {
          method: "POST",
          headers: { "Content-Type": "application/octet-stream" },
          body: file,
        },
      );
      const result = await response.json();
      if (!response.ok)
        throw new Error(
          typeof result.detail === "string"
            ? result.detail
            : "导入失败，请重试。",
        );
      onImported(result);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "导入失败，请重试。");
    } finally {
      setBusy(false);
    }
  }
  return (
    <section className="panel" aria-label="导入教材">
      <label>
        选择 PDF / TXT / Markdown（最大 20 MiB）{" "}
        <input
          type="file"
          accept=".pdf,.txt,.md"
          disabled={busy}
          onChange={(e) => {
            setFile(e.target.files?.[0] ?? null);
            setError("");
          }}
        />
      </label>
      <button disabled={!file || busy} onClick={upload}>
        {busy ? "正在导入…" : "导入教材"}
      </button>
      {busy && <p role="status">正在上传并解析教材，请稍候。</p>}
      {error && <p role="alert">{error}</p>}
    </section>
  );
}
