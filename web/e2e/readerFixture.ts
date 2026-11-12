// Minimal self-contained PDF with three pages and a real outline.
export function readerPdf() {
  const objects = [
    "<< /Type /Catalog /Pages 2 0 R /Outlines 10 0 R >>",
    "<< /Type /Pages /Kids [4 0 R 6 0 R 8 0 R] /Count 3 >>",
    "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
  ];
  for (let page = 1; page <= 3; page++) {
    const stream = `BT /F1 22 Tf 70 730 Td (Reader page ${page}) Tj 0 -40 Td /F1 14 Tf (Original PDF layout and selectable text.) Tj ET\n0.2 0.5 0.3 rg 70 400 250 100 re f\n`;
    objects.push(
      `<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 3 0 R >> >> /Contents ${page * 2 + 3} 0 R >>`,
    );
    objects.push(
      `<< /Length ${Buffer.byteLength(stream)} >>\nstream\n${stream}endstream`,
    );
  }
  objects.push(
    "<< /Type /Outlines /First 11 0 R /Last 12 0 R /Count 2 >>",
    "<< /Title (Chapter one) /Parent 10 0 R /Next 12 0 R /Dest [4 0 R /Fit] >>",
    "<< /Title (Chapter three) /Parent 10 0 R /Prev 11 0 R /Dest [8 0 R /Fit] >>",
  );
  let content = "%PDF-1.4\n";
  const offsets = [0];
  objects.forEach((object, i) => {
    offsets.push(Buffer.byteLength(content));
    content += `${i + 1} 0 obj\n${object}\nendobj\n`;
  });
  const xref = Buffer.byteLength(content);
  content +=
    `xref\n0 ${objects.length + 1}\n0000000000 65535 f \n` +
    offsets
      .slice(1)
      .map((n) => `${String(n).padStart(10, "0")} 00000 n \n`)
      .join("");
  content += `trailer\n<< /Size ${objects.length + 1} /Root 1 0 R >>\nstartxref\n${xref}\n%%EOF\n`;
  return Buffer.from(content);
}
