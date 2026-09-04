"use client";

import { useMemo } from "react";
import ReactMarkdown, { defaultUrlTransform } from "react-markdown";
import remarkGfm from "remark-gfm";

export type InlineCitation = {
  index: number;
  url: string;
};

type MarkdownMessageProps = {
  content: string;
  citations?: InlineCitation[];
  onCitationClick?: (index: number) => void;
};

const CITED_MARKER_RE = /\[(\d{1,2})\]/g;

/** 把答案中的 [n] 转成指向 PDF 原页的引用链接。 */
function linkCitations(content: string, citations: InlineCitation[]) {
  if (!citations.length) return content;
  const available = new Set(citations.map((citation) => citation.index));
  return content.replace(CITED_MARKER_RE, (match, rawIndex: string) => {
    const index = Number(rawIndex);
    return available.has(index) ? `[${rawIndex}](rag-citation:${index})` : match;
  });
}

export function MarkdownMessage({ content, citations, onCitationClick }: MarkdownMessageProps) {
  const processed = useMemo(() => linkCitations(content, citations ?? []), [content, citations]);
  const citationMap = useMemo(
    () => new Map((citations ?? []).map((citation) => [citation.index, citation])),
    [citations],
  );
  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm]}
      urlTransform={(url) => (url.startsWith("rag-citation:") ? url : defaultUrlTransform(url))}
      components={{
        p: ({ children }) => <p className="my-2 first:mt-0 last:mb-0">{children}</p>,
        strong: ({ children }) => <strong className="font-semibold">{children}</strong>,
        ul: ({ children }) => <ul className="my-2 list-disc space-y-1 pl-5">{children}</ul>,
        ol: ({ children }) => <ol className="my-2 list-decimal space-y-1 pl-5">{children}</ol>,
        li: ({ children }) => <li className="pl-1">{children}</li>,
        h1: ({ children }) => <h3 className="mb-2 mt-3 text-base font-semibold">{children}</h3>,
        h2: ({ children }) => <h3 className="mb-2 mt-3 text-base font-semibold">{children}</h3>,
        h3: ({ children }) => <h3 className="mb-2 mt-3 text-sm font-semibold">{children}</h3>,
        code: ({ children }) => (
          <code className="rounded bg-muted px-1 py-0.5 font-mono text-[0.92em]">
            {children}
          </code>
        ),
        pre: ({ children }) => (
          <pre className="my-3 overflow-auto rounded-md border bg-background p-3 text-xs leading-6">
            {children}
          </pre>
        ),
        blockquote: ({ children }) => (
          <blockquote className="my-3 border-l-2 pl-3 text-muted-foreground">
            {children}
          </blockquote>
        ),
        table: ({ children }) => (
          <div className="my-3 overflow-x-auto">
            <table className="w-full border-collapse text-xs">{children}</table>
          </div>
        ),
        th: ({ children }) => (
          <th className="border bg-muted px-2 py-1 text-left font-medium">{children}</th>
        ),
        td: ({ children }) => <td className="border px-2 py-1 align-top">{children}</td>,
        a: ({ children, href }) => {
          if (typeof href === "string" && href.startsWith("rag-citation:")) {
            const citation = citationMap.get(Number(href.slice("rag-citation:".length)));
            if (!citation) return <>{children}</>;
            return (
              <a
                href={citation.url}
                target="_blank"
                rel="noreferrer"
                title="点击查看原文资料"
                onClick={(event) => {
                  if (!onCitationClick) return;
                  event.preventDefault();
                  onCitationClick(citation.index);
                }}
                className="mx-0.5 inline-flex h-5 min-w-5 items-center justify-center rounded border border-primary/40 bg-primary/10 px-1 align-middle text-[0.75em] font-medium text-primary no-underline transition hover:bg-primary/20"
              >
                [{children}]
              </a>
            );
          }
          return (
            <a
              href={href}
              target="_blank"
              rel="noreferrer"
              className="text-primary underline underline-offset-2"
            >
              {children}
            </a>
          );
        },
      }}
    >
      {processed}
    </ReactMarkdown>
  );
}
