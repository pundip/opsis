// A small, safe Markdown renderer for text tiles.
//
// It builds React elements directly and never injects HTML, so text written by
// one person (or the agent) cannot run script in another person's browser.
// Supported: # headings, paragraphs, - and 1. lists, **bold**, *italic*,
// `code` and [links](https://...). Links must be http(s) or mailto.
import type { ReactNode } from "react";

export type Block =
  | { type: "heading"; level: 1 | 2 | 3; text: string }
  | { type: "paragraph"; text: string }
  | { type: "list"; ordered: boolean; items: string[] };

export function parseBlocks(source: string): Block[] {
  const blocks: Block[] = [];
  let paragraph: string[] = [];
  const flush = () => {
    if (paragraph.length) blocks.push({ type: "paragraph", text: paragraph.join(" ") });
    paragraph = [];
  };
  for (const raw of source.replace(/\r\n/g, "\n").split("\n")) {
    const line = raw.trim();
    const heading = /^(#{1,3})\s+(.*)$/.exec(line);
    const bullet = /^[-*]\s+(.*)$/.exec(line);
    const numbered = /^\d+[.)]\s+(.*)$/.exec(line);
    if (!line) {
      flush();
    } else if (heading) {
      flush();
      blocks.push({ type: "heading", level: heading[1]!.length as 1 | 2 | 3, text: heading[2]! });
    } else if (bullet || numbered) {
      flush();
      const ordered = Boolean(numbered);
      const last = blocks.at(-1);
      const text = (bullet ?? numbered)![1]!;
      if (last?.type === "list" && last.ordered === ordered) last.items.push(text);
      else blocks.push({ type: "list", ordered, items: [text] });
    } else {
      paragraph.push(line);
    }
  }
  flush();
  return blocks;
}

export function safeHref(href: string): string | null {
  return /^(https?:\/\/|mailto:)/i.test(href.trim()) ? href.trim() : null;
}

const INLINE = /(\*\*([^*]+)\*\*|\*([^*]+)\*|`([^`]+)`|\[([^\]]+)\]\(([^)\s]+)\))/g;

export function renderInline(text: string): ReactNode[] {
  const nodes: ReactNode[] = [];
  let last = 0;
  for (const match of text.matchAll(INLINE)) {
    if (match.index > last) nodes.push(text.slice(last, match.index));
    const key = match.index;
    if (match[2]) nodes.push(<strong key={key}>{match[2]}</strong>);
    else if (match[3]) nodes.push(<em key={key}>{match[3]}</em>);
    else if (match[4]) nodes.push(<code key={key} className="rounded bg-zinc-500/15 px-1 font-mono text-[0.9em]">{match[4]}</code>);
    else if (match[5]) {
      const href = safeHref(match[6]!);
      nodes.push(
        href ? (
          <a key={key} href={href} target="_blank" rel="noopener noreferrer" className="underline">
            {match[5]}
          </a>
        ) : (
          match[5]
        ),
      );
    }
    last = match.index + match[0].length;
  }
  if (last < text.length) nodes.push(text.slice(last));
  return nodes;
}

const HEADING = ["text-xl font-semibold", "text-lg font-semibold", "text-base font-semibold"];

export function Markdown({ source }: { source: string }) {
  return (
    <div className="space-y-2 text-sm leading-relaxed">
      {parseBlocks(source).map((block, index) => {
        if (block.type === "heading") {
          const Tag = `h${block.level + 1}` as "h2" | "h3" | "h4";
          return <Tag key={index} className={HEADING[block.level - 1]}>{renderInline(block.text)}</Tag>;
        }
        if (block.type === "list") {
          const Tag = block.ordered ? "ol" : "ul";
          return (
            <Tag key={index} className={`space-y-0.5 pl-5 ${block.ordered ? "list-decimal" : "list-disc"}`}>
              {block.items.map((item, i) => (
                <li key={i}>{renderInline(item)}</li>
              ))}
            </Tag>
          );
        }
        return <p key={index}>{renderInline(block.text)}</p>;
      })}
    </div>
  );
}
