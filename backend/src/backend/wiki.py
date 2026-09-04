"""派生 Wiki 层：从已解析原文生成可追溯的文档与概念页面。"""

from __future__ import annotations

import json
import math
import re
from collections import defaultdict
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from .config import Settings
from .domain import Chunk, ParsedDocument, SearchHit
from .prompt import WIKI_ANALYSIS_SYSTEM_PROMPT, WIKI_OVERVIEW_SYSTEM_PROMPT

WIKILINK_RE = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]")
COMPUTED_RELATED_RE = re.compile(
    r"\n## 计算相关\n.*?(?=\n## |\Z)",
    re.DOTALL,
)


class JsonGenerator(Protocol):
    def generate_json(
        self,
        system_prompt: str,
        user_payload: dict[str, object],
        *,
        max_tokens: int = 1200,
    ) -> dict[str, object] | None: ...


class WikiManager:
    """维护 storage/wiki，所有页面均可回链至 parsed 与 chunk。"""

    def __init__(self, settings: Settings, generator: JsonGenerator) -> None:
        self.settings = settings
        self.generator = generator
        self.root = settings.data_dir / "wiki"
        self.documents_dir = self.root / "documents"
        self.concepts_dir = self.root / "concepts"
        self.metadata_dir = self.root / "metadata"
        self.graph_dir = self.root / "graph"
        self._concept_cache: list[dict[str, object]] | None = None
        for path in (self.documents_dir, self.concepts_dir, self.metadata_dir, self.graph_dir):
            path.mkdir(parents=True, exist_ok=True)

    def sync_document(
        self,
        document: dict[str, object],
        parsed: ParsedDocument,
        chunks: Sequence[Chunk],
        *,
        event: str = "ingest",
    ) -> None:
        """基于本次解析产物刷新一个文档的 Wiki 派生页。"""

        if not self.settings.wiki_enabled:
            return
        self._concept_cache = None
        document_id = str(document["id"])
        analysis = self._analyze(document, parsed, chunks)
        metadata: dict[str, object] = {
            "schema_version": 1,
            "generated_at": _utc_now(),
            "document": {
                "id": document_id,
                "title": str(document.get("title") or document.get("filename") or document_id),
                "filename": document.get("filename"),
                "standard_no": document.get("standard_no"),
                "version": document.get("version"),
                "sha256": document.get("sha256"),
                "pages": parsed.pages,
                "parser_name": parsed.parser_name,
            },
            "source": {
                "parsed_path": f"parsed/{document_id}",
                "normalized_path": f"parsed/{document_id}/normalized.json",
                "chunk_count": len(chunks),
            },
            "analysis": analysis,
        }
        self._write_json(self.metadata_dir / f"{document_id}.json", metadata)
        self._write_document_page(metadata)
        self._rebuild_concept_pages()
        self._rebuild_index()
        self._rebuild_overview()
        self._rebuild_related()
        self._append_log(event, metadata)

    def catalog_context(
        self,
        documents: Sequence[dict[str, object]],
    ) -> list[dict[str, object]]:
        """提供给 Planner 的轻量 Wiki 上下文，不携带原文正文。"""

        context: list[dict[str, object]] = []
        for document in documents:
            document_id = str(document["id"])
            metadata = self._read_json(self.metadata_dir / f"{document_id}.json")
            analysis = metadata.get("analysis", {}) if metadata else {}
            if not isinstance(analysis, dict):
                analysis = {}
            concepts = analysis.get("concepts", [])
            context.append(
                {
                    "document_id": document_id,
                    "summary": analysis.get("summary"),
                    "topics": analysis.get("topics", []),
                    "concepts": [
                        concept.get("name")
                        for concept in concepts
                        if isinstance(concept, dict) and concept.get("name")
                    ],
                }
            )
        return context

    def log_query(
        self,
        question: str,
        query_type: str,
        hits: Sequence[SearchHit],
    ) -> None:
        """追加查询事件，不保存回答内容，也不影响正常问答。"""

        if not self.settings.wiki_enabled:
            return
        document_ids = sorted({hit.document_id for hit in hits})
        try:
            self._append_log_entry(
                event="query",
                title=re.sub(r"\s+", " ", question).strip()[:160] or "空问题",
                details=[
                    f"- query_type: {query_type}",
                    f"- hit_count: {len(hits)}",
                    f"- documents: {', '.join(document_ids) if document_ids else 'none'}",
                ],
            )
        except OSError:
            return

    def concept_lexicon(
        self,
        document_ids: Sequence[str] | None = None,
    ) -> list[dict[str, object]]:
        """返回概念词典：name、aliases、anchors（document_id -> chunk_ids）。

        document_ids 提供时只保留在这些文档中有锚点的概念。
        """

        if not self.settings.wiki_enabled:
            return []
        scope = set(document_ids) if document_ids else None
        lexicon: list[dict[str, object]] = []
        for entry in self._all_concepts():
            anchors = entry["anchors"]
            assert isinstance(anchors, dict)
            if scope is not None:
                anchors = {doc_id: ids for doc_id, ids in anchors.items() if doc_id in scope}
            if not anchors:
                continue
            lexicon.append(
                {
                    "name": entry["name"],
                    "aliases": sorted(entry["aliases"]),
                    "anchors": anchors,
                }
            )
        return lexicon

    def expand_query(self, query: str, document_ids: Sequence[str] | None = None) -> str:
        """问题命中概念名或别名时，把该概念其余别名追加进 BM25 查询。

        向量检索语义本身可跨越不同叫法，扩展只服务于关键词召回。
        """

        if not self.settings.wiki_enabled:
            return query
        additions: list[str] = []
        for entry in self.concept_lexicon(document_ids):
            terms = [str(entry["name"]), *entry["aliases"]]
            if not any(len(term) >= 2 and term in query for term in terms):
                continue
            additions.extend(
                alias for alias in entry["aliases"] if len(alias) >= 2 and alias not in query
            )
        additions = list(dict.fromkeys(additions))[:8]
        if not additions:
            return query
        return f"{query} {' '.join(additions)}"

    def search_concept_chunks(
        self,
        query: str,
        document_ids: Sequence[str] | None = None,
        *,
        limit: int = 20,
    ) -> list[str]:
        """返回问题命中概念的 chunk 锚点 id，作为 wiki 概念通道的召回来源。"""

        if not self.settings.wiki_enabled:
            return []
        chunk_ids: list[str] = []
        for entry in self.concept_lexicon(document_ids):
            terms = [str(entry["name"]), *entry["aliases"]]
            if not any(len(term) >= 2 and term in query for term in terms):
                continue
            anchors = entry["anchors"]
            assert isinstance(anchors, dict)
            for document_anchors in anchors.values():
                chunk_ids.extend(document_anchors)
                if len(chunk_ids) >= limit:
                    return chunk_ids[:limit]
        return chunk_ids[:limit]

    def related_pages(self, page_id: str, limit: int = 8) -> list[dict[str, object]]:
        """读取 related 图中与页面相邻的高分关系，供导航与前端展示。"""

        if not self.settings.wiki_enabled:
            return []
        graph = self._read_json(self.graph_dir / "related.json")
        if not graph:
            return []
        relations = [
            relation
            for relation in graph.get("relations", [])
            if isinstance(relation, dict)
            and page_id in (relation.get("source"), relation.get("target"))
        ]
        relations.sort(key=lambda item: float(item.get("score", 0)), reverse=True)
        results: list[dict[str, object]] = []
        for relation in relations[:limit]:
            other = relation["target"] if relation["source"] == page_id else relation["source"]
            results.append(
                {
                    "page_id": other,
                    "score": float(relation.get("score", 0)),
                    "signals": list(relation.get("signals", [])),
                }
            )
        return results

    def concepts_overview(self) -> list[dict[str, object]]:
        """概念总览：每个概念的别名、关联文档与锚点数量，供知识网络浏览。"""

        documents = self._document_titles()
        overview: list[dict[str, object]] = []
        for entry in self._all_concepts():
            anchors = entry["anchors"]
            assert isinstance(anchors, dict)
            if not anchors:
                continue
            document_refs = [
                {
                    "document_id": document_id,
                    "title": documents.get(document_id, document_id),
                    "anchor_count": len(chunk_ids),
                }
                for document_id, chunk_ids in anchors.items()
            ]
            document_refs.sort(key=lambda item: -int(item["anchor_count"]))
            overview.append(
                {
                    "name": entry["name"],
                    "aliases": sorted(entry["aliases"]),
                    "documents": document_refs,
                    "total_anchors": sum(len(ids) for ids in anchors.values()),
                }
            )
        overview.sort(key=lambda item: -int(item["total_anchors"]))
        return overview

    def graph_summary(
        self,
        *,
        min_score: float = 0.0,
        max_links: int = 400,
    ) -> dict[str, object]:
        """related 图的节点与边摘要，节点带人类可读标题。"""

        if not self.settings.wiki_enabled:
            return {"nodes": [], "links": []}
        graph = self._read_json(self.graph_dir / "related.json") or {}
        relations = [
            relation
            for relation in graph.get("relations", [])
            if isinstance(relation, dict) and float(relation.get("score", 0)) >= min_score
        ]
        relations.sort(key=lambda item: float(item.get("score", 0)), reverse=True)
        relations = relations[:max_links]
        documents = self._document_titles()
        node_ids = {str(relation["source"]) for relation in relations} | {
            str(relation["target"]) for relation in relations
        }
        nodes = [
            {"page_id": node_id, "title": self._page_title(node_id, documents)}
            for node_id in sorted(node_ids)
        ]
        links = [
            {
                "source": str(relation["source"]),
                "target": str(relation["target"]),
                "score": round(float(relation.get("score", 0)), 2),
                "signals": list(relation.get("signals", [])),
            }
            for relation in relations
        ]
        return {"nodes": nodes, "links": links}

    def read_page(self, page_id: str) -> dict[str, object] | None:
        """读取 Wiki 派生页 markdown；page_id 形如 concepts/<名称>、documents/<id>、overview。"""

        if not self.settings.wiki_enabled:
            return None
        normalized = page_id.strip("/")
        parts = normalized.split("/")
        if normalized == "overview":
            path = self.root / "overview.md"
        elif len(parts) == 2 and parts[0] in {"concepts", "documents"}:
            path = self.root / parts[0] / f"{parts[1]}.md"
        else:
            return None
        try:
            resolved = path.resolve()
            resolved.relative_to(self.root.resolve())
        except (OSError, ValueError):
            return None
        if not resolved.exists() or not resolved.is_file():
            return None
        text = resolved.read_text(encoding="utf-8")
        title_match = re.search(r"^#\s+(.+)$", text, re.MULTILINE)
        title = title_match.group(1).strip() if title_match else parts[-1]
        return {"page_id": normalized, "title": title, "markdown": text}

    @staticmethod
    def _page_title(page_id: str, documents: dict[str, str]) -> str:
        parts = page_id.split("/")
        if parts and parts[0] == "concepts" and len(parts) == 2:
            return parts[1]
        if parts and parts[0] == "documents" and len(parts) == 2:
            return documents.get(parts[1], parts[1])
        return page_id

    def _document_titles(self) -> dict[str, str]:
        titles: dict[str, str] = {}
        for metadata in self._metadata_items():
            document = metadata.get("document")
            if isinstance(document, dict) and document.get("id"):
                titles[str(document["id"])] = str(
                    document.get("title") or document.get("filename") or document["id"]
                )
        return titles

    def _all_concepts(self) -> list[dict[str, object]]:
        """聚合全部 metadata 中的概念：别名合并、锚点按文档分组。"""

        if self._concept_cache is not None:
            return self._concept_cache
        entries: dict[str, dict[str, object]] = {}
        for metadata in self._metadata_items():
            document = metadata.get("document")
            analysis = metadata.get("analysis")
            if not isinstance(document, dict) or not isinstance(analysis, dict):
                continue
            document_id = str(document.get("id"))
            for concept in analysis.get("concepts", []):
                if not isinstance(concept, dict) or not concept.get("name"):
                    continue
                name = str(concept["name"])
                entry = entries.setdefault(
                    name,
                    {"name": name, "aliases": set(), "anchors": {}},
                )
                aliases = entry["aliases"]
                assert isinstance(aliases, set)
                aliases.update(str(alias) for alias in concept.get("aliases", []))
                anchors = entry["anchors"]
                assert isinstance(anchors, dict)
                existing = anchors.setdefault(document_id, [])
                existing.extend(str(chunk_id) for chunk_id in concept.get("source_chunk_ids", []))
        self._concept_cache = list(entries.values())
        return self._concept_cache

    def _analyze(
        self,
        document: dict[str, object],
        parsed: ParsedDocument,
        chunks: Sequence[Chunk],
    ) -> dict[str, object]:
        fallback = _fallback_analysis(document, chunks)
        if not self.settings.wiki_llm_enabled:
            return fallback
        batches = _chunk_batches(chunks, self.settings.wiki_analysis_max_source_chars)
        if not batches:
            return fallback
        batches = batches[: self.settings.wiki_analysis_max_batches]
        known_concepts = set(self._known_concepts())
        document_identity = _document_identity_keys(document)
        merged: dict[str, object] | None = None
        for index, batch in enumerate(batches):
            generated = self.generator.generate_json(
                WIKI_ANALYSIS_SYSTEM_PROMPT,
                {
                    "document": {
                        "id": document.get("id"),
                        "title": document.get("title"),
                        "standard_no": document.get("standard_no"),
                        "version": document.get("version"),
                        "pages": parsed.pages,
                    },
                    "batch": f"{index + 1}/{len(batches)}",
                    "source_chunks": batch["samples"],
                    "known_concepts": sorted(known_concepts),
                },
                max_tokens=4000,
            )
            if generated is None:
                # 首批失败说明 LLM 不可用或输出无效，直接回退；
                # 后续批失败则保留已合并结果，避免整篇回退。
                if merged is None:
                    return fallback
                break
            part = _normalize_analysis(
                generated,
                fallback,
                batch["ref_map"],
                known_concepts,
                document_identity,
            )
            merged = part if merged is None else _merge_analyses(merged, part)
            known_concepts |= {str(item["name"]) for item in part["concepts"]}
        if merged is None:
            return fallback
        concepts = [
            item
            for item in merged["concepts"]
            if isinstance(item, dict) and item.get("name")
        ]
        concepts.sort(key=lambda item: len(item.get("source_chunk_ids", [])), reverse=True)
        merged["concepts"] = concepts[:40]
        return merged

    def _write_document_page(self, metadata: dict[str, object]) -> None:
        document = metadata["document"]
        analysis = metadata["analysis"]
        assert isinstance(document, dict)
        assert isinstance(analysis, dict)
        document_id = str(document["id"])
        concepts = [
            item
            for item in analysis.get("concepts", [])
            if isinstance(item, dict) and item.get("name")
        ]
        lines = [
            "---",
            "type: standard",
            f"document_id: {document_id}",
            f"standard_no: {document.get('standard_no') or ''}",
            f"version: {document.get('version') or ''}",
            f"source: ../parsed/{document_id}/normalized.json",
            "status: generated",
            "---",
            "",
            f"# {document['title']}",
            "",
            str(analysis.get("summary") or "尚未生成文档摘要。"),
            "",
            "## 主题",
            "",
        ]
        lines.extend(f"- {topic}" for topic in analysis.get("topics", []) if isinstance(topic, str))
        lines.extend(["", "## 相关概念", ""])
        lines.extend(f"- [[{concept['name']}]]" for concept in concepts)
        lines.extend(["", "## 原文锚点", ""])
        for concept in concepts:
            anchors = ", ".join(str(chunk_id) for chunk_id in concept.get("source_chunk_ids", []))
            if anchors:
                lines.append(f"- {concept['name']}：`{anchors}`")
        self._write_text(self.documents_dir / f"{document_id}.md", "\n".join(lines) + "\n")

    def _rebuild_concept_pages(self) -> None:
        grouped: dict[str, list[tuple[dict[str, object], dict[str, object]]]] = defaultdict(list)
        for metadata_path in self.metadata_dir.glob("*.json"):
            metadata = self._read_json(metadata_path)
            if not metadata:
                continue
            document = metadata.get("document")
            analysis = metadata.get("analysis")
            if not isinstance(document, dict) or not isinstance(analysis, dict):
                continue
            for concept in analysis.get("concepts", []):
                if isinstance(concept, dict) and isinstance(concept.get("name"), str):
                    grouped[concept["name"]].append((document, concept))

        for name, entries in grouped.items():
            aliases = _unique_strings(
                alias for _, item in entries for alias in item.get("aliases", [])
            )
            dimensions = _unique_strings(
                dimension for _, item in entries for dimension in item.get("dimensions", [])
            )
            related_concepts = _unique_strings(
                target
                for _, item in entries
                for target in item.get("related_concepts", [])
                if target != name
            )
            lines = [
                "---",
                "type: concept",
                f"title: {name}",
                "status: generated_needs_review",
                "---",
                "",
                f"# {name}",
                "",
                "## 别名",
                "",
            ]
            lines.extend(f"- {alias}" for alias in aliases)
            lines.extend(["", "## 可比较维度", ""])
            lines.extend(f"- {dimension}" for dimension in dimensions)
            lines.extend(["", "## 关联规范与原文锚点", ""])
            for document, concept in entries:
                title = document.get("title") or document.get("filename") or document.get("id")
                source_chunk_ids = concept.get("source_chunk_ids", [])
                anchors = ", ".join(str(chunk_id) for chunk_id in source_chunk_ids)
                lines.append(f"- [[documents/{document['id']}|{title}]]：`{anchors}`")
            lines.extend(["", "## 显式交叉引用", ""])
            lines.extend(f"- [[{target}]]" for target in related_concepts)
            self._write_text(self.concepts_dir / f"{_safe_name(name)}.md", "\n".join(lines) + "\n")

    def _rebuild_index(self) -> None:
        metadata_items = [
            item for path in self.metadata_dir.glob("*.json") if (item := self._read_json(path))
        ]
        lines = [
            "# 规智库 Wiki 索引",
            "",
            "本目录为 `storage/parsed` 派生的知识导航层；最终问答结论必须回到原文检索片段核验。",
            "",
            "## 规范文档",
            "",
        ]
        for item in sorted(
            metadata_items,
            key=lambda value: str(value["document"].get("title", "")),
        ):
            document = item["document"]
            assert isinstance(document, dict)
            title = document.get("title") or document["id"]
            lines.append(f"- [[documents/{document['id']}|{title}]]")
        lines.extend(["", "## 概念页面", ""])
        for path in sorted(self.concepts_dir.glob("*.md")):
            lines.append(f"- [[concepts/{path.stem}|{path.stem}]]")
        self._write_text(self.root / "index.md", "\n".join(lines) + "\n")

    def _rebuild_overview(self) -> None:
        metadata_items = self._metadata_items()
        concept_names = self._known_concepts()
        topic_counts: dict[str, int] = defaultdict(int)
        for item in metadata_items:
            analysis = item.get("analysis", {})
            if not isinstance(analysis, dict):
                continue
            for topic in analysis.get("topics", []):
                if isinstance(topic, str) and topic.strip():
                    topic_counts[topic.strip()] += 1
        fallback = {
            "summary": (
                f"当前 Wiki 包含 {len(metadata_items)} 份规范文档和 "
                f"{len(concept_names)} 个概念页面。"
            ),
            "themes": [
                name
                for name, _ in sorted(topic_counts.items(), key=lambda item: (-item[1], item[0]))[
                    :8
                ]
            ],
            "gaps": ["概念与文档关系均为自动生成，尚待原文核验与人工审核。"],
        }
        generated = None
        if self.settings.wiki_llm_enabled and metadata_items:
            generated = self.generator.generate_json(
                WIKI_OVERVIEW_SYSTEM_PROMPT,
                {
                    "documents": [
                        {
                            "id": item["document"].get("id"),
                            "title": item["document"].get("title"),
                            "standard_no": item["document"].get("standard_no"),
                            "summary": item["analysis"].get("summary"),
                            "topics": item["analysis"].get("topics", []),
                            "concepts": [
                                concept.get("name")
                                for concept in item["analysis"].get("concepts", [])
                                if isinstance(concept, dict)
                            ],
                        }
                        for item in metadata_items
                    ]
                },
                max_tokens=700,
            )
        overview = _normalize_overview(generated, fallback)
        lines = [
            "---",
            "type: overview",
            f"generated_at: {_utc_now()}",
            "status: generated_needs_review",
            "---",
            "",
            "# 规智库知识库概览",
            "",
            str(overview["summary"]),
            "",
            "## 范围统计",
            "",
            f"- 规范文档：{len(metadata_items)}",
            f"- 概念页面：{len(concept_names)}",
            "",
            "## 主要主题",
            "",
        ]
        lines.extend(f"- [[{theme}]]" for theme in overview["themes"])
        lines.extend(["", "## 待完善", ""])
        lines.extend(f"- {gap}" for gap in overview["gaps"])
        self._write_text(self.root / "overview.md", "\n".join(lines) + "\n")

    def _rebuild_related(self) -> None:
        pages = self._wiki_pages()
        relations = _related_scores(pages)
        self._write_json(
            self.graph_dir / "related.json",
            {
                "generated_at": _utc_now(),
                "model": {
                    "direct_wikilink": 3.0,
                    "source_overlap": 4.0,
                    "adamic_adar": 1.5,
                    "type_affinity": 1.0,
                },
                "pages": [
                    {"id": page["id"], "type": page["type"], "path": page["path"]} for page in pages
                ],
                "relations": relations,
            },
        )
        by_page: dict[str, list[dict[str, object]]] = defaultdict(list)
        for relation in relations:
            by_page[str(relation["source"])].append(relation)
            by_page[str(relation["target"])].append(relation)
        for page in pages:
            related = sorted(
                by_page.get(str(page["id"]), []),
                key=lambda item: -float(item["score"]),
            )[:8]
            if not related:
                continue
            lines = ["", "## 计算相关", ""]
            for relation in related:
                other_id = (
                    relation["target"] if relation["source"] == page["id"] else relation["source"]
                )
                other = next(item for item in pages if item["id"] == other_id)
                signals = "、".join(relation["signals"])
                lines.append(
                    f"- [[{other['target']}|{other['title']}]]"
                    f"（{float(relation['score']):.2f}；{signals}）"
                )
            page_path = Path(str(page["path"]))
            current = page_path.read_text(encoding="utf-8")
            current = COMPUTED_RELATED_RE.sub("", current).rstrip()
            page_path.write_text(current + "\n" + "\n".join(lines) + "\n", encoding="utf-8")

    def _append_log(self, event: str, metadata: dict[str, object]) -> None:
        document = metadata["document"]
        analysis = metadata["analysis"]
        assert isinstance(document, dict)
        assert isinstance(analysis, dict)
        concepts = [
            str(item.get("name"))
            for item in analysis.get("concepts", [])
            if isinstance(item, dict) and item.get("name")
        ]
        self._append_log_entry(
            event=event,
            title=str(document.get("title") or document["id"]),
            details=[
                f"- document_id: `{document['id']}`",
                f"- source: `parsed/{document['id']}/normalized.json`",
                f"- concepts: {', '.join(concepts) if concepts else 'none'}",
            ],
        )

    def _append_log_entry(self, event: str, title: str, details: Sequence[str]) -> None:
        path = self.root / "log.md"
        if not path.exists():
            self._write_text(
                path,
                "# Wiki 事件日志\n\n"
                "此文件仅追加记录入库、查询和 Lint 事件；不作为规范事实来源。\n\n",
            )
        lines = [f"## [{_utc_now()}] {event} | {title}", *details, ""]
        with path.open("a", encoding="utf-8") as stream:
            stream.write("\n".join(lines))

    def _known_concepts(self) -> list[str]:
        names: list[str] = []
        for item in self._metadata_items():
            analysis = item.get("analysis", {})
            if not isinstance(analysis, dict):
                continue
            names.extend(
                str(concept["name"])
                for concept in analysis.get("concepts", [])
                if isinstance(concept, dict) and concept.get("name")
            )
        return sorted(set(names))

    def _metadata_items(self) -> list[dict[str, Any]]:
        return [
            item for path in self.metadata_dir.glob("*.json") if (item := self._read_json(path))
        ]

    def _wiki_pages(self) -> list[dict[str, object]]:
        pages: list[dict[str, object]] = []
        document_ids = {path.stem for path in self.documents_dir.glob("*.md")}
        for path, page_type in (
            *((path, "document") for path in self.documents_dir.glob("*.md")),
            *((path, "concept") for path in self.concepts_dir.glob("*.md")),
        ):
            text = path.read_text(encoding="utf-8")
            page_id = f"{path.parent.name}/{path.stem}"
            if page_type == "document":
                metadata = self._read_json(self.metadata_dir / f"{path.stem}.json") or {}
                document = metadata.get("document", {})
                title = (
                    document.get("title")
                    or document.get("standard_no")
                    or path.stem
                    if isinstance(document, dict)
                    else path.stem
                )
            else:
                title = path.stem
            pages.append(
                {
                    "id": page_id,
                    "target": page_id,
                    "link": path.stem,
                    "title": str(title),
                    "type": page_type,
                    "path": str(path),
                    "links": [match.strip() for match in WIKILINK_RE.findall(text)],
                    "sources": [path.stem]
                    if page_type == "document"
                    else _linked_document_ids(text, document_ids),
                }
            )
        return pages

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any] | None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def _write_json(path: Path, payload: dict[str, object]) -> None:
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    def _write_text(path: Path, text: str) -> None:
        path.write_text(text, encoding="utf-8")


def _chunk_batches(
    chunks: Sequence[Chunk],
    char_limit: int,
) -> list[dict[str, object]]:
    """按章节顺序把 chunks 切成多批样本，每批内同章节最多 2 个片段。

    LLM 只输出短编号引用（c1、c2…），由 ref_map 映射回真实 chunk_id，
    避免 UUID 撑爆输出 token 导致 JSON 截断。
    """

    batches: list[dict[str, object]] = []
    samples: list[dict[str, object]] = []
    ref_map: dict[str, str] = {}
    chapter_counts: dict[str, int] = defaultdict(int)
    total = 0
    for position, chunk in enumerate(chunks, 1):
        text = re.sub(r"\s+", " ", chunk.text).strip()[:700]
        if not text:
            continue
        chapter = chunk.chapter_path or ""
        if chapter_counts.get(chapter, 0) >= 2:
            continue
        if total + len(text) > char_limit and samples:
            batches.append({"samples": samples, "ref_map": ref_map})
            samples = []
            ref_map = {}
            chapter_counts = defaultdict(int)
            total = 0
        reference = f"c{position}"
        samples.append(
            {
                "chunk_id": reference,
                "chapter_path": chunk.chapter_path,
                "clause_no": chunk.clause_no,
                "pages": [chunk.page_start, chunk.page_end],
                "text": text,
            }
        )
        ref_map[reference] = chunk.id
        chapter_counts[chapter] += 1
        total += len(text)
    if samples:
        batches.append({"samples": samples, "ref_map": ref_map})
    return batches


def _fallback_analysis(document: dict[str, object], chunks: Sequence[Chunk]) -> dict[str, object]:
    topics = _unique_strings(chunk.chapter_path for chunk in chunks if chunk.chapter_path)[:12]
    title = document.get("title") or document.get("filename") or "当前文档"
    return {
        "summary": f"{title}：已完成结构化解析，共 {len(chunks)} 个检索片段。",
        "topics": topics,
        "concepts": [],
        "generation": "fallback",
    }


def _normalize_analysis(
    generated: dict[str, object] | None,
    fallback: dict[str, object],
    chunk_ref_map: dict[str, str],
    known_concepts: set[str],
    document_identity: set[str],
) -> dict[str, object]:
    if not generated:
        return fallback
    summary = generated.get("summary")
    topics = _unique_strings(generated.get("topics", []))[:12]
    concepts: list[dict[str, object]] = []
    raw_concepts = generated.get("concepts", [])
    if isinstance(raw_concepts, list):
        for item in raw_concepts[:20]:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name or _norm_key(name) in document_identity:
                continue
            raw_anchors = item.get("source_chunk_ids", [])
            anchors = (
                [
                    chunk_ref_map[str(chunk_id)]
                    for chunk_id in raw_anchors
                    if str(chunk_id) in chunk_ref_map
                ]
                if isinstance(raw_anchors, list)
                else []
            )
            if not anchors:
                continue
            concepts.append(
                {
                    "name": name[:80],
                    "aliases": _unique_strings(item.get("aliases", []))[:8],
                    "dimensions": _unique_strings(item.get("dimensions", []))[:8],
                    "source_chunk_ids": list(dict.fromkeys(anchors)),
                    "related_concepts": _unique_strings(item.get("related_concepts", []))[:8],
                }
            )
    valid_concepts = known_concepts | {str(item["name"]) for item in concepts}
    for concept in concepts:
        concept["related_concepts"] = [
            target
            for target in concept["related_concepts"]
            if target in valid_concepts and target != concept["name"]
        ]
    return {
        "summary": (
            str(summary).strip()[:180]
            if isinstance(summary, str) and summary.strip()
            else fallback["summary"]
        ),
        "topics": topics or fallback["topics"],
        "concepts": concepts,
        "generation": "llm",
    }


def _merge_analyses(base: dict[str, object], extra: dict[str, object]) -> dict[str, object]:
    """合并两批分析结果：概念按名称合并，别名、维度、锚点和关系取并集。"""

    merged: dict[str, dict[str, object]] = {}
    for concept in base.get("concepts", []):
        if isinstance(concept, dict) and concept.get("name"):
            merged[str(concept["name"])] = dict(concept)
    for concept in extra.get("concepts", []):
        if not (isinstance(concept, dict) and concept.get("name")):
            continue
        name = str(concept["name"])
        existing = merged.get(name)
        if existing is None:
            merged[name] = dict(concept)
            continue
        for key in ("aliases", "dimensions", "source_chunk_ids", "related_concepts"):
            combined = _unique_strings(
                list(existing.get(key, [])) + list(concept.get(key, []))
            )
            existing[key] = combined[:20] if key == "source_chunk_ids" else combined[:8]
    return {
        "summary": base.get("summary") or extra.get("summary"),
        "topics": _unique_strings(
            list(base.get("topics", [])) + list(extra.get("topics", []))
        )[:16],
        "concepts": list(merged.values()),
        "generation": "llm",
    }


def _norm_key(value: object) -> str:
    return re.sub(r"\s+", "", str(value or "")).lower()


def _document_identity_keys(document: dict[str, object]) -> set[str]:
    """文档标题、标准号等不应被抽成概念，用于过滤 LLM 输出。"""

    filename = str(document.get("filename") or "")
    keys: set[str] = set()
    for value in (
        document.get("title"),
        document.get("standard_no"),
        Path(filename).stem if filename else None,
    ):
        key = _norm_key(value)
        if len(key) >= 4:
            keys.add(key)
    return keys


def _normalize_overview(
    generated: dict[str, object] | None,
    fallback: dict[str, object],
) -> dict[str, object]:
    if not generated:
        return fallback
    summary = generated.get("summary")
    return {
        "summary": str(summary).strip()[:220]
        if isinstance(summary, str) and summary.strip()
        else fallback["summary"],
        "themes": _unique_strings(generated.get("themes", []))[:8] or fallback["themes"],
        "gaps": _unique_strings(generated.get("gaps", []))[:8] or fallback["gaps"],
    }


def _linked_document_ids(text: str, known_document_ids: set[str]) -> list[str]:
    document_ids: list[str] = []
    for target in WIKILINK_RE.findall(text):
        target = target.strip()
        if target.startswith("documents/"):
            target = target.removeprefix("documents/")
        if target in known_document_ids:
            document_ids.append(target)
    return _unique_strings(document_ids)


def _related_scores(pages: Sequence[dict[str, object]]) -> list[dict[str, object]]:
    by_id = {str(page["id"]): page for page in pages}
    by_link: dict[str, str] = {}
    for page in pages:
        page_id = str(page["id"])
        aliases = {str(page["id"]), str(page["target"]), str(page["link"])}
        if str(page["type"]) == "document":
            aliases.add(page_id.removeprefix("documents/"))
        else:
            aliases.add(str(page["title"]))
        for alias in aliases:
            by_link[alias] = page_id
    adjacency: dict[str, set[str]] = {page_id: set() for page_id in by_id}
    for page in pages:
        source = str(page["id"])
        for target_name in page["links"]:
            target = by_link.get(str(target_name))
            if target and target != source:
                adjacency[source].add(target)
                adjacency[target].add(source)

    relations: list[dict[str, object]] = []
    page_ids = sorted(by_id)
    for index, source in enumerate(page_ids):
        for target in page_ids[index + 1 :]:
            source_page = by_id[source]
            target_page = by_id[target]
            signals: list[str] = []
            score = 0.0
            if target in adjacency[source]:
                score += 3.0
                signals.append("direct_wikilink")
            source_docs = set(str(item) for item in source_page["sources"])
            target_docs = set(str(item) for item in target_page["sources"])
            if source_docs & target_docs:
                score += 4.0
                signals.append("source_overlap")
            common_neighbors = adjacency[source] & adjacency[target]
            adamic_adar = sum(
                1 / math.log(len(adjacency[node]))
                for node in common_neighbors
                if len(adjacency[node]) > 1
            )
            if adamic_adar:
                score += 1.5 * adamic_adar
                signals.append("adamic_adar")
            if signals and source_page["type"] == target_page["type"]:
                score += 1.0
                signals.append("type_affinity")
            if score:
                relations.append(
                    {
                        "source": source,
                        "target": target,
                        "score": round(score, 4),
                        "signals": signals,
                    }
                )
    return sorted(relations, key=lambda item: float(item["score"]), reverse=True)


def _unique_strings(values: object) -> list[str]:
    if not isinstance(values, Iterable) or isinstance(values, (str, bytes, dict)):
        return []
    return list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))


def _safe_name(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', "_", name).strip()[:100] or "untitled"


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()
