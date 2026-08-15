/**
 * A small markdown renderer.
 *
 * Models write markdown whether you ask them to or not, so a reply
 * currently arrives full of literal asterisks and backticks. This turns the
 * common subset into elements: headings, lists, fenced code, inline code,
 * bold and italic.
 *
 * WHY NOT react-markdown.
 *
 * It is a good library and it would be a reasonable choice. This is about
 * a hundred lines, handles what a chat reply actually contains, and adds no
 * dependency to audit or update -- the same trade CLAUDE.md makes when it
 * asks before adding anything.
 *
 * WHY THERE IS NO XSS HOLE HERE.
 *
 * The dangerous way to render markdown is to build an HTML string and hand
 * it to dangerouslySetInnerHTML, because then any HTML in the model's
 * output -- or in a web page it just fetched and quoted -- becomes live
 * markup in your page. Nothing below produces HTML. It produces React
 * elements with the text as a child, and React escapes text children. A
 * reply containing <script> renders as the characters "<script>".
 *
 * That matters more here than in most chat apps: since Phase 10 this
 * assistant can fetch arbitrary pages, so stranger-written text reaches
 * this renderer as a matter of course.
 */

import { ReactNode } from "react";

/** Split on fenced code blocks, keeping both halves in order. */
function splitFences(text: string): { code: boolean; lang?: string; body: string }[] {
  const parts: { code: boolean; lang?: string; body: string }[] = [];
  const pattern = /```([\w+-]*)\n?([\s\S]*?)```/g;

  let index = 0;
  let match: RegExpExecArray | null;

  while ((match = pattern.exec(text)) !== null) {
    if (match.index > index) {
      parts.push({ code: false, body: text.slice(index, match.index) });
    }
    parts.push({ code: true, lang: match[1] || undefined, body: match[2] });
    index = match.index + match[0].length;
  }

  // An unterminated fence -- which happens constantly while streaming,
  // since the closing ``` has not arrived yet. Treat the rest as code so
  // the block does not flicker between prose and code as it types out.
  const rest = text.slice(index);
  if (rest) {
    const open = rest.match(/```([\w+-]*)\n?([\s\S]*)$/);
    if (open) {
      if (open.index && open.index > 0) {
        parts.push({ code: false, body: rest.slice(0, open.index) });
      }
      parts.push({ code: true, lang: open[1] || undefined, body: open[2] });
    } else {
      parts.push({ code: false, body: rest });
    }
  }

  return parts;
}

/** Bold, italic and inline code within a single line. */
function inline(text: string, keyPrefix: string): ReactNode[] {
  const nodes: ReactNode[] = [];
  // One pass, alternation ordered so ** is tried before *.
  const pattern = /(`[^`]+`)|(\*\*[^*]+\*\*)|(\*[^*]+\*)|(_[^_]+_)/g;

  let index = 0;
  let match: RegExpExecArray | null;
  let n = 0;

  while ((match = pattern.exec(text)) !== null) {
    if (match.index > index) nodes.push(text.slice(index, match.index));
    const token = match[0];
    const key = `${keyPrefix}-${n++}`;

    if (token.startsWith("`")) {
      nodes.push(<code key={key}>{token.slice(1, -1)}</code>);
    } else if (token.startsWith("**")) {
      nodes.push(<strong key={key}>{token.slice(2, -2)}</strong>);
    } else {
      nodes.push(<em key={key}>{token.slice(1, -1)}</em>);
    }
    index = match.index + token.length;
  }

  if (index < text.length) nodes.push(text.slice(index));
  return nodes;
}

/** Turn a run of prose into headings, lists and paragraphs. */
function prose(text: string, keyPrefix: string): ReactNode[] {
  const blocks: ReactNode[] = [];
  const lines = text.split("\n");

  let paragraph: string[] = [];
  let list: { ordered: boolean; items: string[] } | null = null;
  let n = 0;

  function flushParagraph() {
    if (!paragraph.length) return;
    const body = paragraph.join(" ").trim();
    if (body) {
      blocks.push(<p key={`${keyPrefix}-p${n++}`}>{inline(body, `${keyPrefix}-p${n}`)}</p>);
    }
    paragraph = [];
  }

  function flushList() {
    if (!list) return;
    const items = list.items.map((item, i) => (
      <li key={i}>{inline(item, `${keyPrefix}-li${n}-${i}`)}</li>
    ));
    blocks.push(
      list.ordered ? (
        <ol key={`${keyPrefix}-l${n++}`}>{items}</ol>
      ) : (
        <ul key={`${keyPrefix}-l${n++}`}>{items}</ul>
      ),
    );
    list = null;
  }

  for (const line of lines) {
    const heading = line.match(/^(#{1,4})\s+(.*)$/);
    const bullet = line.match(/^\s*[-*+]\s+(.*)$/);
    const numbered = line.match(/^\s*\d+[.)]\s+(.*)$/);

    if (heading) {
      flushParagraph();
      flushList();
      // Capped at h4 and rendered small: a model writing "# Summary" in a
      // chat reply does not mean a page title.
      const level = Math.min(heading[1].length + 2, 6);
      const Tag = `h${level}` as "h3" | "h4" | "h5" | "h6";
      blocks.push(<Tag key={`${keyPrefix}-h${n++}`}>{inline(heading[2], `${keyPrefix}-h`)}</Tag>);
    } else if (bullet) {
      flushParagraph();
      if (!list || list.ordered) {
        flushList();
        list = { ordered: false, items: [] };
      }
      list.items.push(bullet[1]);
    } else if (numbered) {
      flushParagraph();
      if (!list || !list.ordered) {
        flushList();
        list = { ordered: true, items: [] };
      }
      list.items.push(numbered[1]);
    } else if (line.trim() === "") {
      flushParagraph();
      flushList();
    } else {
      flushList();
      paragraph.push(line);
    }
  }

  flushParagraph();
  flushList();
  return blocks;
}

/** Render a model reply. */
export function Markdown({ text }: { text: string }) {
  return (
    <>
      {splitFences(text).map((part, i) =>
        part.code ? (
          <pre className="code-block" key={i}>
            {part.lang && <span className="code-lang">{part.lang}</span>}
            <code>{part.body}</code>
          </pre>
        ) : (
          <div className="prose" key={i}>
            {prose(part.body, `b${i}`)}
          </div>
        ),
      )}
    </>
  );
}
