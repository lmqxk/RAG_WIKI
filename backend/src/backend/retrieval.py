"""检索编排模块：理解查询意图，融合条款定位、全文检索和向量检索结果。"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Sequence

from .config import Settings
from .domain import SearchHit
from .providers import RerankProvider
from .repository import Repository
from .vector_index import VectorIndex

CLAUSE_QUERY_RE = re.compile(r"(?<!\d)(\d+(?:\.\d+){2,5})(?!\d)")
COMPARISON_WORDS = ("对比", "比较", "区别", "差异", "变化", "新旧", "新版", "旧版")


def query_type(question: str) -> str:
    if any(word in question for word in COMPARISON_WORDS):
        return "comparison"
    if CLAUSE_QUERY_RE.search(question):
        return "clause"
    if any(word in question for word in ("有哪些", "总结", "汇总", "要求")):
        return "summary"
    return "fact"


def focused_query(question: str, kind: str) -> str:
    if kind != "comparison":
        return question
    focused = re.sub(r"《[^》]+》", " ", question)
    for noise in (
        "对比",
        "比较",
        "相同点",
        "变化",
        "差异",
        "区别",
        "有哪些",
        "分别",
        "与",
        "和",
    ):
        focused = focused.replace(noise, " ")
    focused = re.sub(r"[，。；：、？！,.!?;:]+", " ", focused)
    focused = re.sub(r"\s+", " ", focused).strip()
    if "附设" in focused and "建筑内" not in focused:
        focused = focused.replace("附设", "附设在建筑内的", 1)
    return focused or question


def reciprocal_rank_fusion(
    ranked_lists: Sequence[Sequence[SearchHit]],
    *,
    k: int = 60,
) -> list[SearchHit]:
    scores: dict[str, float] = defaultdict(float)
    hits: dict[str, SearchHit] = {}
    for ranked in ranked_lists:
        for rank, hit in enumerate(ranked, 1):
            scores[hit.chunk_id] += 1 / (k + rank)
            if hit.chunk_id not in hits or hit.source == "exact":
                hits[hit.chunk_id] = hit
    for chunk_id, score in scores.items():
        hits[chunk_id].score = score
    return sorted(hits.values(), key=lambda item: item.score, reverse=True)


class HybridRetriever:
    def __init__(
        self,
        settings: Settings,
        repository: Repository,
        vector_index: VectorIndex,
        reranker: RerankProvider,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.vector_index = vector_index
        self.reranker = reranker

    def retrieve(
        self,
        question: str,
        document_ids: Sequence[str] | None,
    ) -> tuple[str, list[SearchHit]]:
        kind = query_type(question)
        search_question = focused_query(question, kind)
        if kind == "comparison" and document_ids and len(document_ids) > 1:
            per_document = [
                self._retrieve_scope(search_question, [document_id])
                for document_id in document_ids
            ]
            final = self._balance_documents(
                [hit for document_hits in per_document for hit in document_hits]
            )
            return kind, final
        final = self._retrieve_scope(search_question, document_ids)
        if kind == "comparison":
            final = self._balance_documents(final)
        return kind, final

    def _retrieve_scope(
        self,
        question: str,
        document_ids: Sequence[str] | None,
    ) -> list[SearchHit]:
        bm25 = self.repository.bm25_search(
            question,
            document_ids=document_ids,
            limit=self.settings.retrieval_bm25_top_k,
        )
        vector_ranked = self.vector_index.search(
            question,
            document_ids=document_ids,
            limit=self.settings.retrieval_dense_top_k,
        )
        dense = self.repository.get_chunks([chunk_id for chunk_id, _ in vector_ranked])
        vector_scores = dict(vector_ranked)
        for hit in dense:
            hit.score = vector_scores[hit.chunk_id]
            hit.source = "dense"

        ranked_lists: list[list[SearchHit]] = [bm25, dense]
        if not self.vector_index.embeddings.external:
            # 离线哈希向量只用于补召回，BM25 对精确技术术语更可靠。
            ranked_lists.insert(0, bm25)
        clause_match = CLAUSE_QUERY_RE.search(question)
        if clause_match:
            exact = self.repository.exact_clause_search(
                clause_match.group(1),
                document_ids=document_ids,
            )
            ranked_lists.insert(0, exact)
        fused = reciprocal_rank_fusion(ranked_lists)[: self.settings.retrieval_fused_top_k]
        final = self.reranker.rerank(
            question,
            fused,
            self.settings.retrieval_final_top_k,
        ).hits
        return final

    def _balance_documents(self, hits: list[SearchHit]) -> list[SearchHit]:
        by_document: dict[str, list[SearchHit]] = defaultdict(list)
        for hit in hits:
            by_document[hit.document_id].append(hit)
        if len(by_document) < 2:
            return hits
        balanced: list[SearchHit] = []
        while len(balanced) < self.settings.retrieval_final_top_k:
            added = False
            for document_hits in by_document.values():
                if document_hits:
                    balanced.append(document_hits.pop(0))
                    added = True
                if len(balanced) >= self.settings.retrieval_final_top_k:
                    break
            if not added:
                break
        return balanced
