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

    def _analyze(
        self,
        document: dict[str, object],
        parsed: ParsedDocument,
        chunks: Sequence[Chunk],
    ) -> dict[str, object]:
        samples = _chunk_samples(chunks, self.settings.wiki_analysis_max_source_chars)
        fallback = _fallback_analysis(document, chunks)
        if not self.settings.wiki_llm_enabled or not samples:
            return fallback
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
                "source_chunks": samples,
                "known_concepts": self._known_concepts(),
            },
            max_tokens=1400,
        )
        allowed_chunk_ids = {str(sample["chunk_id"]) for sample in samples}
        return _normalize_analysis(
            generated,
            fallback,
            allowed_chunk_ids,
            set(self._known_concepts()),
        )

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


def _chunk_samples(chunks: Sequence[Chunk], char_limit: int) -> list[dict[str, object]]:
    samples: list[dict[str, object]] = []
    seen_chapters: set[str] = set()
    total = 0
    for chunk in chunks:
        if chunk.chapter_path in seen_chapters and len(samples) >= 8:
            continue
        text = re.sub(r"\s+", " ", chunk.text).strip()[:700]
        if not text:
            continue
        if total + len(text) > char_limit and samples:
            break
        samples.append(
            {
                "chunk_id": chunk.id,
                "chapter_path": chunk.chapter_path,
                "clause_no": chunk.clause_no,
                "pages": [chunk.page_start, chunk.page_end],
                "text": text,
            }
        )
        seen_chapters.add(chunk.chapter_path)
        total += len(text)
    return samples


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
    allowed_chunk_ids: set[str],
    known_concepts: set[str],
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
            raw_anchors = item.get("source_chunk_ids", [])
            anchors = (
                [str(chunk_id) for chunk_id in raw_anchors if str(chunk_id) in allowed_chunk_ids]
                if isinstance(raw_anchors, list)
                else []
            )
            if not name or not anchors:
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
