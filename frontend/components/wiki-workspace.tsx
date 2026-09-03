"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  BookMarked,
  FileSearch,
  Link2,
  Search,
  Tag,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Skeleton } from "@/components/ui/skeleton";

type WikiConceptSummary = {
  name: string;
  aliases: string[];
  documents: { document_id: string; title: string; anchor_count: number }[];
  total_anchors: number;
};

type WikiConceptAnchor = {
  chunk_id: string;
  clause_no: string | null;
  chapter_path: string;
  page_start: number;
  page_end: number;
  text: string;
};

type WikiConceptDetail = {
  name: string;
  aliases: string[];
  documents: {
    document_id: string;
    title: string;
    standard_no: string | null;
    anchors: WikiConceptAnchor[];
  }[];
  related: { page_id: string; score: number; signals: string[] }[];
};

export function WikiWorkspace({ apiBase }: { apiBase: string }) {
  const [concepts, setConcepts] = useState<WikiConceptSummary[] | null>(null);
  const [keyword, setKeyword] = useState("");
  const [selected, setSelected] = useState<WikiConceptDetail | null>(null);
  const [loadingDetail, setLoadingDetail] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetch(`${apiBase}/api/wiki/concepts`)
      .then(async (response) => {
        if (!response.ok) throw new Error("知识网络尚未生成，请先完成文档解析。");
        return response.json();
      })
      .then((data: WikiConceptSummary[]) => setConcepts(data))
      .catch((reason: Error) => setError(reason.message));
  }, [apiBase]);

  const loadConcept = useCallback(
    async (name: string) => {
      setLoadingDetail(true);
      setError(null);
      try {
        const response = await fetch(
          `${apiBase}/api/wiki/concepts/${encodeURIComponent(name)}`,
        );
        if (!response.ok) throw new Error("概念详情加载失败。");
        setSelected(await response.json());
      } catch (reason) {
        setError(reason instanceof Error ? reason.message : "概念详情加载失败");
      } finally {
        setLoadingDetail(false);
      }
    },
    [apiBase],
  );

  const filtered = useMemo(() => {
    if (!concepts) return [];
    const query = keyword.trim().toLowerCase();
    if (!query) return concepts;
    return concepts.filter((concept) =>
      [concept.name, ...concept.aliases]
        .join(" ")
        .toLowerCase()
        .includes(query),
    );
  }, [concepts, keyword]);

  const relatedConcepts = useMemo(
    () =>
      (selected?.related ?? [])
        .map((relation) => decodeURIComponent(relation.page_id).replace(/^concepts\//, ""))
        .filter((name) => name && !name.includes("/")),
    [selected],
  );

  return (
    <div className="grid min-w-0 gap-5 xl:grid-cols-[360px_minmax(0,1fr)]">
      <Card className="min-w-0 shadow-none">
        <CardHeader className="pb-3">
          <CardTitle className="flex items-center gap-2 text-base">
            <BookMarked className="size-4 text-primary" />
            概念索引
          </CardTitle>
          <CardDescription>
            从已解析规范中抽取的专业概念，按原文锚点数排序
          </CardDescription>
          <div className="relative mt-2">
            <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
            <Input
              value={keyword}
              onChange={(event) => setKeyword(event.target.value)}
              placeholder="搜索概念或别名…"
              className="pl-9"
            />
          </div>
        </CardHeader>
        <CardContent className="p-0">
          <ScrollArea className="h-[calc(100vh-360px)] min-h-[420px]">
            <div className="flex flex-col gap-2 p-4 pt-0">
              {concepts === null && !error && (
                <>
                  {Array.from({ length: 6 }).map((_, index) => (
                    <Skeleton key={index} className="h-16 w-full" />
                  ))}
                </>
              )}
              {filtered.map((concept) => (
                <button
                  key={concept.name}
                  type="button"
                  onClick={() => void loadConcept(concept.name)}
                  className={`rounded-xl border p-3 text-left transition hover:border-primary/40 hover:bg-muted/40 ${
                    selected?.name === concept.name ? "border-primary/60 bg-primary/5" : ""
                  }`}
                >
                  <span className="flex items-center justify-between gap-2">
                    <span className="truncate text-sm font-medium">{concept.name}</span>
                    <Badge variant="outline" className="shrink-0 text-[10px]">
                      {concept.total_anchors} 锚点
                    </Badge>
                  </span>
                  {concept.aliases.length > 0 && (
                    <span className="mt-1 block truncate text-xs text-muted-foreground">
                      {concept.aliases.join(" · ")}
                    </span>
                  )}
                  <span className="mt-2 flex flex-wrap gap-1">
                    {concept.documents.map((document) => (
                      <Badge
                        key={document.document_id}
                        variant="secondary"
                        className="max-w-full truncate text-[10px]"
                      >
                        {document.title}
                      </Badge>
                    ))}
                  </span>
                </button>
              ))}
              {concepts !== null && filtered.length === 0 && (
                <p className="rounded-xl border border-dashed p-4 text-sm text-muted-foreground">
                  没有匹配的概念，试试更短的关键词。
                </p>
              )}
              {error && (
                <p className="rounded-xl border border-dashed p-4 text-sm text-muted-foreground">
                  {error}
                </p>
              )}
            </div>
          </ScrollArea>
        </CardContent>
      </Card>

      <div className="min-w-0">
        {!selected && !loadingDetail ? (
          <Card className="shadow-none">
            <CardContent className="flex min-h-[420px] flex-col items-center justify-center p-6 text-center">
              <div className="flex size-12 items-center justify-center rounded-2xl bg-primary/10 text-primary">
                <Tag className="size-6" />
              </div>
              <h3 className="mt-4 font-semibold">选择一个概念查看知识网络</h3>
              <p className="mt-2 max-w-md text-sm leading-6 text-muted-foreground">
                每个概念页会展示它在各规范中的原文锚点（条款号、章节、PDF 页码），
                以及与其他概念、规范的关联关系。
              </p>
            </CardContent>
          </Card>
        ) : loadingDetail ? (
          <Card className="shadow-none">
            <CardContent className="flex flex-col gap-4 p-6">
              <Skeleton className="h-6 w-40" />
              <Skeleton className="h-4 w-full" />
              <Skeleton className="h-4 w-3/4" />
              <Skeleton className="h-32 w-full" />
            </CardContent>
          </Card>
        ) : (
          selected && (
            <Card className="min-w-0 shadow-none">
              <CardHeader>
                <CardTitle className="text-xl">{selected.name}</CardTitle>
                {selected.aliases.length > 0 && (
                  <CardDescription>别名：{selected.aliases.join(" · ")}</CardDescription>
                )}
                <div className="mt-1 flex flex-wrap gap-2">
                  <Badge variant="secondary">
                    {selected.documents.length} 份规范 · {relatedConcepts.length} 个关联概念
                  </Badge>
                </div>
              </CardHeader>
              <CardContent className="flex flex-col gap-5">
                {selected.documents.map((document) => (
                  <section key={document.document_id} className="min-w-0">
                    <div className="mb-2 flex flex-wrap items-center gap-2">
                      <h4 className="text-sm font-semibold">
                        {document.standard_no || document.title}
                      </h4>
                      <Badge variant="outline">{document.anchors.length} 处原文</Badge>
                    </div>
                    <div className="flex flex-col gap-2">
                      {document.anchors.slice(0, 6).map((anchor) => (
                        <a
                          key={anchor.chunk_id}
                          href={`${apiBase}/api/documents/${document.document_id}/file#page=${anchor.page_start}`}
                          target="_blank"
                          rel="noreferrer"
                          className="group rounded-lg border p-3 transition hover:border-primary/40"
                        >
                          <span className="flex items-center justify-between gap-3">
                            <span className="truncate font-mono text-xs text-primary">
                              {anchor.clause_no || anchor.chapter_path || "相关条款"}
                            </span>
                            <span className="shrink-0 text-xs text-muted-foreground group-hover:text-primary">
                              PDF 第 {anchor.page_start} 页
                            </span>
                          </span>
                          <span className="mt-1 block line-clamp-2 text-xs leading-5 text-muted-foreground">
                            {anchor.text}
                          </span>
                        </a>
                      ))}
                      {document.anchors.length > 6 && (
                        <a
                          href={`${apiBase}/api/documents/${document.document_id}/file#page=${document.anchors[6].page_start}`}
                          target="_blank"
                          rel="noreferrer"
                          className="text-xs text-primary underline underline-offset-2"
                        >
                          查看其余 {document.anchors.length - 6} 处原文（跳到 PDF）
                        </a>
                      )}
                    </div>
                  </section>
                ))}

                {relatedConcepts.length > 0 && (
                  <section>
                    <h4 className="mb-2 flex items-center gap-2 text-sm font-semibold">
                      <Link2 className="size-4 text-primary" />
                      关联概念
                    </h4>
                    <div className="flex flex-wrap gap-2">
                      {relatedConcepts.map((name) => (
                        <Button
                          key={name}
                          variant="outline"
                          size="sm"
                          type="button"
                          onClick={() => void loadConcept(name)}
                        >
                          {name}
                        </Button>
                      ))}
                    </div>
                  </section>
                )}

                {!selected.documents.length && (
                  <p className="flex items-center gap-2 rounded-xl border border-dashed p-4 text-sm text-muted-foreground">
                    <FileSearch className="size-4" />
                    该概念还没有原文锚点，等待下一次索引重建。
                  </p>
                )}
              </CardContent>
            </Card>
          )
        )}
      </div>
    </div>
  );
}
