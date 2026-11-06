export function Highlight({ text, quote }: { text: string; quote?: string }) {
  if (!quote) return <>{text}</>;
  const start = text.indexOf(quote);
  if (start < 0 || text.indexOf(quote, start + 1) >= 0)
    return (
      <>
        <span className="notice">引用无法唯一匹配此文本块。</span>
        {text}
      </>
    );
  return (
    <>
      {text.slice(0, start)}
      <mark>{quote}</mark>
      {text.slice(start + quote.length)}
    </>
  );
}
