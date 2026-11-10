type Node = { type: string; value?: string; url?: string; children?: Node[] };

// Runs after Markdown parsing, so fenced and inline code remain literal.
export function cardLinks() {
  return (tree: Node) => {
    function walk(node: Node) {
      if (!node.children || ["link", "code", "inlineCode"].includes(node.type))
        return;
      node.children = node.children.flatMap((child) => {
        if (child.type !== "text") {
          walk(child);
          return [child];
        }
        const text = child.value || "";
        const result: Node[] = [];
        let offset = 0;
        for (const match of text.matchAll(
          /\[\[(card_[a-z0-9]+)\|([^\]\n]*)\]\]/g,
        )) {
          result.push({ type: "text", value: text.slice(offset, match.index) });
          result.push({
            type: "link",
            url: `?view=personal&card=${match[1]}`,
            children: [{ type: "text", value: match[2] }],
          });
          offset = match.index! + match[0].length;
        }
        result.push({ type: "text", value: text.slice(offset) });
        return result;
      });
    }
    walk(tree);
  };
}
