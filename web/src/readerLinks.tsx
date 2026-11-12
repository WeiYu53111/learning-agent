export function readerUrl(
  book: string,
  location?: { page?: number | null; start_line?: number | null },
  chunk?: string,
  section?: string,
) {
  const params = new URLSearchParams({ view: "reader", book });
  if (location?.page != null) params.set("page", String(location.page));
  else if (location?.start_line != null)
    params.set("line", String(location.start_line));
  else if (chunk) params.set("chunk", chunk);
  else if (section) params.set("section", section);
  return `/?${params}`;
}
export function OriginalLink({
  book,
  location,
  chunk,
  section,
}: {
  book: string;
  location?: { page?: number | null; start_line?: number | null };
  chunk?: string;
  section?: string;
}) {
  return (
    <a
      href={readerUrl(book, location, chunk, section)}
      target="_blank"
      rel="noopener noreferrer"
    >
      查看原文件 ↗
    </a>
  );
}
