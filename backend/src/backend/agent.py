"""受控 Agentic RAG 编排层：规划多步检索、补充检索，并复用现有混合检索与重排。"""

from __future__ import annotations

import json
import re
import time
from collections import defaultdict
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Literal

import httpx
from pydantic import BaseModel, Field, ValidationError

from .config import Settings
from .domain import SearchHit
from .prompt import PLANNER_SYSTEM_PROMPT
from .repository import Repository
from .retrieval import (
    HybridRetriever,
    QueryPlan,
    focused_query,
    normalize_identifier,
    plan_query,
)
from .wiki import WikiManager

ALLOWED_TOOLS = {"search_general", "search_in_document", "search_wiki"}


AllowedTool = Literal["search_general", "search_in_document", "search_wiki"]
Intent = Literal["comparison", "single_query", "exploration"]


class SearchStep(BaseModel):
    tool: AllowedTool = Field(..., description="允许的工具名称")
    query: str = Field(
        ...,
        min_length=1,
        max_length=120,
        description="由 LLM 根据问题上下文自由生成的检索 query，不允许固定模板拼接",
    )
    document_ref: str | None = Field(
        None,
        description=(
            "search_in_document 必填。可填写 allowed_docs 中的 id、standard_no、title 或 filename"
        ),
    )
    dimension: str = Field(
        ...,
        min_length=1,
        max_length=40,
        description="该 query 覆盖的信息维度",
    )
    reason: str | None = Field(None, max_length=120, description="选择该检索步骤的原因")


class PlanningResult(BaseModel):
    intent: Intent = Field(..., description="用户问题意图")
    query_rewrites: list[str] = Field(
        ...,
        min_length=1,
        max_length=6,
        description="LLM 自由生成的多角度 query 改写列表，覆盖不同信息维度",
    )
    steps: list[SearchStep] = Field(
        ...,
        min_length=1,
        max_length=8,
        description="受控工具执行步骤",
    )


@dataclass(slots=True)
class AgentStep:
    tool: str
    query: str
    document_ids: list[str] | None
    reason: str


@dataclass(slots=True)
class AgentRun:
    kind: str
    hits: list[SearchHit]
    steps: list[AgentStep]
    supplemented: bool
    planner_ms: float | None = None
    recall_ms: float | None = None
    rerank_ms: float | None = None


@dataclass(slots=True)
class PlannerResult:
    plan: QueryPlan
    steps: list[AgentStep]
    external: bool


class AgenticRetriever:
    def __init__(
        self,
        settings: Settings,
        repository: Repository,
        retriever: HybridRetriever,
        wiki: WikiManager | None = None,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.retriever = retriever
        self.wiki = wiki

    def retrieve(
        self,
        question: str,
        document_ids: Sequence[str] | None,
    ) -> tuple[str, list[SearchHit]]:
        result = self.run(question, document_ids)
        return result.kind, result.hits

    def run(
        self,
        question: str,
        document_ids: Sequence[str] | None,
    ) -> AgentRun:
        if not self.settings.agentic_retrieval_enabled:
            started_at = time.perf_counter()
            query = self.wiki.expand_query(question, document_ids) if self.wiki else question
            kind, hits = self.retriever.retrieve(query, document_ids)
            if self.wiki:
                self.wiki.log_query(question, kind, hits)
            return AgentRun(
                kind=kind,
                hits=hits,
                steps=[],
                supplemented=False,
                recall_ms=(time.perf_counter() - started_at) * 1000,
            )

        planner_started_at = time.perf_counter()
        planned = self._plan_steps(question, document_ids)
        planner_ms = (time.perf_counter() - planner_started_at) * 1000
        plan = planned.plan
        steps = planned.steps
        recall_started_at = time.perf_counter()
        hits = self._execute_steps(steps)
        supplemented = False

        if self._needs_supplement(plan, hits):
            supplement_steps = self._supplement_steps(question, plan, hits)
            remaining = max(self.settings.agentic_max_steps - len(steps), 0)
            supplement_steps = supplement_steps[:remaining]
            if supplement_steps:
                steps.extend(supplement_steps)
                hits.extend(self._execute_steps(supplement_steps))
                supplemented = True
        recall_ms = (time.perf_counter() - recall_started_at) * 1000

        rerank_started_at = time.perf_counter()
        final_hits = self._finalize(question, plan.kind, hits)
        rerank_ms = (time.perf_counter() - rerank_started_at) * 1000
        if self.wiki:
            self.wiki.log_query(question, plan.kind, final_hits)

        return AgentRun(
            kind=plan.kind,
            hits=final_hits,
            steps=steps,
            supplemented=supplemented,
            planner_ms=planner_ms,
            recall_ms=recall_ms,
            rerank_ms=rerank_ms,
        )

    def _plan_steps(
        self,
        question: str,
        document_ids: Sequence[str] | None,
    ) -> PlannerResult:
        documents = self.repository.list_documents()
        plan = plan_query(question, document_ids, documents)
        llm_steps = self._llm_initial_steps(question, plan, documents)
        if llm_steps:
            return PlannerResult(plan=plan, steps=llm_steps, external=True)
        return PlannerResult(plan=plan, steps=self._initial_steps(plan), external=False)

    def _llm_initial_steps(
        self,
        question: str,
        plan: QueryPlan,
        documents: Sequence[dict[str, object]],
    ) -> list[AgentStep]:
        if not (
            self.settings.agentic_planner_llm_enabled
            and self.settings.openai_api_key
            and self.settings.chat_model
        ):
            return []
        ready_documents = [document for document in documents if document.get("status") == "READY"]
        allowed_document_ids = set(
            plan.target_document_ids or [str(doc["id"]) for doc in ready_documents]
        )
        document_payload = [
            {
                "id": str(document["id"]),
                "title": document.get("title"),
                "standard_no": document.get("standard_no"),
                "version": document.get("version"),
                "filename": document.get("filename"),
            }
            for document in ready_documents
            if str(document["id"]) in allowed_document_ids
        ]
        if not document_payload:
            return []
        scoped_documents = [
            document for document in ready_documents if str(document["id"]) in allowed_document_ids
        ]
        wiki_context = self.wiki.catalog_context(scoped_documents) if self.wiki else []
        user = {
            "question": question,
            "query_type_hint": plan.kind,
            "focused_query_hint": plan.search_question,
            "max_steps": self.settings.agentic_max_steps,
            "allowed_docs": document_payload,
            "wiki_context": wiki_context,
            "output_schema": {
                "intent": "comparison | single_query | exploration",
                "query_rewrites": ["多角度自由改写 query，覆盖不同信息维度"],
                "steps": [
                    {
                        "tool": "search_general | search_in_document | search_wiki",
                        "query": "由 LLM 自主生成的具体检索 query，最长 120 字",
                        "document_ref": (
                            "search_in_document 必填，可用 id、standard_no、title 或 filename"
                        ),
                        "dimension": "该 query 覆盖的信息维度",
                        "reason": "为什么这样检索",
                    }
                ],
            },
        }
        try:
            response = httpx.post(
                f"{self.settings.openai_base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {self.settings.openai_api_key}"},
                json={
                    "model": self.settings.chat_model,
                    "temperature": 0,
                    "max_tokens": 1200,
                    "thinking": {"type": "disabled"},
                    "messages": [
                        {"role": "system", "content": PLANNER_SYSTEM_PROMPT},
                        {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
                    ],
                },
                timeout=60,
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
        except (httpx.HTTPError, KeyError, TypeError):
            return []
        payload = _loads_json_object(str(content))
        if not payload:
            return []
        try:
            planner_result = PlanningResult.model_validate(payload)
        except ValidationError:
            return []
        return self._steps_from_llm_plan(
            planner_result,
            plan,
            ready_documents,
            allowed_document_ids,
        )

    def _steps_from_llm_plan(
        self,
        planner_result: PlanningResult,
        plan: QueryPlan,
        documents: Sequence[dict[str, object]],
        allowed_document_ids: set[str],
    ) -> list[AgentStep]:
        steps: list[AgentStep] = []
        used_search_wiki = False
        for planned_step in planner_result.steps:
            tool = planned_step.tool
            query = _clean_query(planned_step.query)
            reason = planned_step.reason or planned_step.dimension
            if tool not in ALLOWED_TOOLS or not query:
                continue
            if tool == "search_wiki":
                if used_search_wiki or not self.wiki:
                    continue
                used_search_wiki = True
                steps.append(
                    AgentStep(tool=tool, query=query, document_ids=None, reason=reason)
                )
                continue
            if tool == "search_in_document":
                document_id = _resolve_document_ref(planned_step.document_ref, documents)
                if not document_id or document_id not in allowed_document_ids:
                    continue
                document_ids = [document_id]
            else:
                document_ids = plan.target_document_ids
            steps.append(
                AgentStep(
                    tool=tool,
                    query=query,
                    document_ids=document_ids,
                    reason=reason,
                )
            )
            if len(steps) >= self.settings.agentic_max_steps:
                break
        return steps

    def _initial_steps(self, plan: QueryPlan) -> list[AgentStep]:
        if plan.kind == "comparison" and plan.target_document_ids:
            return [
                AgentStep(
                    tool="search_in_document",
                    query=plan.search_question,
                    document_ids=[document_id],
                    reason="对比问题先分文档检索，保证每份目标文档都有独立召回机会。",
                )
                for document_id in plan.target_document_ids
            ][: self.settings.agentic_max_steps]
        return [
            AgentStep(
                tool="search_general",
                query=plan.search_question,
                document_ids=plan.target_document_ids,
                reason="普通问题使用当前作用域内的混合检索。",
            )
        ]

    def _execute_steps(self, steps: Sequence[AgentStep]) -> list[SearchHit]:
        if not steps:
            return []
        if len(steps) == 1 or self.settings.agentic_parallel_workers <= 1:
            return self._execute_step(steps[0])
        max_workers = min(
            len(steps),
            max(1, self.settings.agentic_parallel_workers),
        )
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            results = list(executor.map(self._execute_step, steps))
        hits: list[SearchHit] = []
        for step_hits in results:
            hits.extend(step_hits)
        return hits

    def _execute_step(self, step: AgentStep) -> list[SearchHit]:
        if step.tool == "search_wiki":
            return self._execute_search_wiki(step)
        query = step.query
        if self.wiki:
            query = self.wiki.expand_query(query, step.document_ids)
        return self.retriever._retrieve_scope(query, step.document_ids, rerank=False)

    def _execute_search_wiki(self, step: AgentStep) -> list[SearchHit]:
        assert self.wiki is not None
        chunk_ids = self.wiki.search_concept_chunks(step.query, step.document_ids)
        if not chunk_ids:
            return []
        hits = self.repository.get_chunks(chunk_ids)
        for hit in hits:
            hit.score = 0.5
            hit.source = "wiki"
        return hits

    def _needs_supplement(self, plan: QueryPlan, hits: Sequence[SearchHit]) -> bool:
        if not hits:
            return True
        if len(hits) < self.settings.agentic_min_hits:
            return True
        if plan.kind != "comparison":
            return False
        if not plan.target_document_ids or len(plan.target_document_ids) < 2:
            return False
        covered = {hit.document_id for hit in hits}
        required = set(plan.target_document_ids)
        return len(covered & required) < min(2, len(required))

    def _supplement_steps(
        self,
        question: str,
        plan: QueryPlan,
        hits: Sequence[SearchHit],
    ) -> list[AgentStep]:
        queries = self._supplement_queries(question, plan.search_question)
        if plan.kind == "comparison" and plan.target_document_ids:
            covered = {hit.document_id for hit in hits}
            target_ids = [doc_id for doc_id in plan.target_document_ids if doc_id not in covered]
            if not target_ids:
                target_ids = list(plan.target_document_ids)
            return [
                AgentStep(
                    tool="search_in_document",
                    query=query,
                    document_ids=[document_id],
                    reason="资料覆盖不足，换关键词在目标文档内补充检索。",
                )
                for document_id in target_ids
                for query in queries
            ]
        return [
            AgentStep(
                tool="search_general",
                query=query,
                document_ids=plan.target_document_ids,
                reason="初次召回资料不足，换关键词补充检索。",
            )
            for query in queries
        ]

    def _supplement_queries(self, question: str, search_question: str) -> list[str]:
        candidates = [
            search_question,
            question,
            focused_query(question, "comparison"),
        ]
        cleaned = [_clean_query(query) for query in candidates]
        return list(dict.fromkeys(query for query in cleaned if query))

    def _finalize(
        self,
        question: str,
        kind: str,
        hits: Sequence[SearchHit],
    ) -> list[SearchHit]:
        deduped = _dedupe_hits(hits)
        if not deduped:
            return []
        reranked = self.retriever.reranker.rerank(
            question,
            deduped[: self.settings.retrieval_fused_top_k],
            self.settings.retrieval_final_top_k,
        ).hits
        if kind == "comparison":
            return self.retriever._balance_documents(reranked)
        return reranked


def _clean_query(query: str) -> str:
    return re.sub(r"\s+", " ", query).strip()


def _loads_json_object(content: str) -> dict[str, object] | None:
    text = content.strip()
    fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence_match:
        text = fence_match.group(1)
    else:
        object_match = re.search(r"\{.*\}", text, re.DOTALL)
        if object_match:
            text = object_match.group(0)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _resolve_document_ref(
    document_ref: object,
    documents: Sequence[dict[str, object]],
) -> str | None:
    normalized_ref = normalize_identifier(document_ref)
    if not normalized_ref:
        return None
    for document in documents:
        document_id = str(document["id"])
        identifiers = [
            document_id,
            document.get("title"),
            document.get("standard_no"),
            document.get("version"),
            document.get("filename"),
        ]
        for identifier in identifiers:
            normalized_identifier = normalize_identifier(identifier)
            if normalized_identifier and (
                normalized_ref == normalized_identifier
                or normalized_ref in normalized_identifier
                or normalized_identifier in normalized_ref
            ):
                return document_id
    return None


def _dedupe_hits(hits: Sequence[SearchHit]) -> list[SearchHit]:
    by_chunk: dict[str, SearchHit] = {}
    first_order: dict[str, int] = {}
    for index, hit in enumerate(hits):
        first_order.setdefault(hit.chunk_id, index)
        existing = by_chunk.get(hit.chunk_id)
        if existing is None or hit.score > existing.score:
            by_chunk[hit.chunk_id] = hit
    by_document: dict[str, int] = defaultdict(int)
    for hit in by_chunk.values():
        by_document[hit.document_id] += 1
    return sorted(
        by_chunk.values(),
        key=lambda hit: (
            -hit.score,
            by_document[hit.document_id],
            first_order[hit.chunk_id],
        ),
    )
