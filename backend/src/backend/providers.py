"""模型服务模块：封装向量化、重排和兼容 OpenAI 协议的回答生成接口。"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Iterator
from dataclasses import dataclass

import httpx

from .config import Settings
from .domain import SearchHit

TOKEN_RE = re.compile(r"[A-Za-z0-9]+(?:\.[A-Za-z0-9]+)*|[\u3400-\u9fff]")
ASCII_TOKEN_RE = re.compile(r"[A-Za-z0-9]+(?:\.[A-Za-z0-9]+)*")
HAN_SEQUENCE_RE = re.compile(r"[\u3400-\u9fff]+")
IMAGE_PATH_RE = re.compile(r"images/[^\s\"'<>，。；)）]+", re.IGNORECASE)


def lexical_tokens(text: str) -> set[str]:
    tokens = {token.lower() for token in ASCII_TOKEN_RE.findall(text)}
    for sequence in HAN_SEQUENCE_RE.findall(text):
        if len(sequence) == 1:
            tokens.add(sequence)
        else:
            tokens.update(
                sequence[index : index + 2] for index in range(len(sequence) - 1)
            )
    return tokens


def evidence_text_for_llm(text: str) -> str:
    """隐藏内部图片文件路径，避免模型把存储路径当成答案输出。"""

    return IMAGE_PATH_RE.sub("[图片见资料预览]", text)


def evidence_type(hit: SearchHit) -> str:
    return "规范正文" if hit.content_type.startswith("normative") else "条文说明"


def evidence_item(index: int, hit: SearchHit) -> dict[str, object]:
    return {
        "id": index,
        "document": hit.document_title,
        "standard_no": hit.standard_no,
        "version": hit.version,
        "location": {
            "chapter_path": hit.chapter_path or None,
            "clause_no": hit.clause_no,
            "pdf_page": hit.page_start,
        },
        "type": evidence_type(hit),
        "text": evidence_text_for_llm(hit.text),
    }


def evidence_payload(question: str, hits: list[SearchHit], query_type: str) -> object:
    if query_type != "comparison":
        return [evidence_item(index, hit) for index, hit in enumerate(hits, 1)]

    grouped: dict[str, dict[str, object]] = {}
    for index, hit in enumerate(hits, 1):
        group = grouped.setdefault(
            hit.document_id,
            {
                "document_id": hit.document_id,
                "document": hit.document_title,
                "standard_no": hit.standard_no,
                "version": hit.version,
                "items": [],
            },
        )
        items = group["items"]
        assert isinstance(items, list)
        items.append(evidence_item(index, hit))
    return {
        "topic": question,
        "mode": "cross_document_comparison",
        "documents": list(grouped.values()),
    }


class EmbeddingProvider:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.external = bool(settings.openai_api_key and settings.embedding_model)

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if self.external:
            return self._external_embed(texts)
        return [self._hash_embed(text) for text in texts]

    def _external_embed(self, texts: list[str]) -> list[list[float]]:
        assert self.settings.openai_api_key
        assert self.settings.embedding_model
        response = httpx.post(
            f"{self.settings.openai_base_url.rstrip('/')}/embeddings",
            headers={"Authorization": f"Bearer {self.settings.openai_api_key}"},
            json={
                "model": self.settings.embedding_model,
                "input": texts,
                "dimensions": self.settings.embedding_dimension,
            },
            timeout=120,
        )
        response.raise_for_status()
        payload = response.json()
        vectors = [
            item["embedding"] for item in sorted(payload["data"], key=lambda item: item["index"])
        ]
        if any(len(vector) != self.settings.embedding_dimension for vector in vectors):
            raise ValueError("Embedding 返回维度与 RAG_EMBEDDING_DIMENSION 不一致")
        return vectors

    def _hash_embed(self, text: str) -> list[float]:
        dimension = self.settings.embedding_dimension
        vector = [0.0] * dimension
        tokens = TOKEN_RE.findall(text.lower())
        features = [*tokens]
        features.extend(f"{tokens[index]}::{tokens[index + 1]}" for index in range(len(tokens) - 1))
        for feature in features:
            digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
            bucket = int.from_bytes(digest[:4], "little") % dimension
            sign = 1.0 if digest[4] & 1 else -1.0
            vector[bucket] += sign
        norm = math.sqrt(sum(value * value for value in vector)) or 1.0
        return [value / norm for value in vector]


@dataclass(slots=True)
class RerankResult:
    hits: list[SearchHit]
    external: bool


class RerankProvider:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.external = bool(
            settings.rerank_base_url and settings.rerank_api_key and settings.rerank_model
        )

    def rerank(self, query: str, hits: list[SearchHit], top_n: int) -> RerankResult:
        if not hits:
            return RerankResult([], self.external)
        if self.external:
            try:
                return RerankResult(self._external_rerank(query, hits, top_n), True)
            except httpx.HTTPError:
                return RerankResult(self._local_rerank(query, hits, top_n), False)
        return RerankResult(self._local_rerank(query, hits, top_n), False)

    def _local_rerank(
        self,
        query: str,
        hits: list[SearchHit],
        top_n: int,
    ) -> list[SearchHit]:
        query_tokens = lexical_tokens(query)
        for hit in hits:
            hit_tokens = lexical_tokens(hit.text)
            overlap = len(query_tokens & hit_tokens) / max(len(query_tokens), 1)
            exact_bonus = 0.35 if hit.source == "exact" else 0
            normative_bonus = 0.18 if hit.content_type.startswith("normative") else 0
            hit.score = hit.score * 0.65 + overlap * 0.35 + exact_bonus + normative_bonus
        return sorted(hits, key=lambda item: item.score, reverse=True)[:top_n]

    def _external_rerank(
        self,
        query: str,
        hits: list[SearchHit],
        top_n: int,
    ) -> list[SearchHit]:
        assert self.settings.rerank_base_url
        assert self.settings.rerank_api_key
        assert self.settings.rerank_model
        response = httpx.post(
            self.settings.rerank_base_url,
            headers={"Authorization": f"Bearer {self.settings.rerank_api_key}"},
            json={
                "model": self.settings.rerank_model,
                "query": query,
                "documents": [hit.text for hit in hits],
                "top_n": top_n,
                "return_documents": False,
            },
            timeout=120,
        )
        response.raise_for_status()
        results = response.json().get("results", [])
        ranked: list[SearchHit] = []
        for item in results:
            index = int(item["index"])
            if 0 <= index < len(hits):
                hit = hits[index]
                hit.score = float(item.get("relevance_score", item.get("score", 0)))
                ranked.append(hit)
        return ranked[:top_n]


class ChatProvider:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.external = bool(settings.openai_api_key and settings.chat_model)

    def answer(
        self,
        question: str,
        hits: list[SearchHit],
        query_type: str,
    ) -> str:
        if not hits:
            return "当前知识库中没有检索到足以支持结论的条款，请补充规范名称、版本或适用条件。"
        if self.external:
            return self._external_answer(question, hits, query_type).strip()
        return self._extractive_answer(question, hits, query_type)

    def _external_answer(
        self,
        question: str,
        hits: list[SearchHit],
        query_type: str,
    ) -> str:
        payload = self._chat_payload(question, hits, query_type)
        response = httpx.post(
            f"{self.settings.openai_base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {self.settings.openai_api_key}"},
            json=payload,
            timeout=180,
        )
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"].strip()

    def answer_stream(
        self,
        question: str,
        hits: list[SearchHit],
        query_type: str,
    ) -> Iterator[str]:
        if not hits:
            yield "当前知识库中没有检索到足以支持结论的条款，请补充规范名称、版本或适用条件。"
            return
        if not self.external:
            yield self._extractive_answer(question, hits, query_type)
            return
        assert self.settings.openai_api_key
        assert self.settings.chat_model
        payload = self._chat_payload(question, hits, query_type)
        payload["stream"] = True
        with httpx.stream(
            "POST",
            f"{self.settings.openai_base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {self.settings.openai_api_key}"},
            json=payload,
            timeout=180,
        ) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                if not line or not line.startswith("data: "):
                    continue
                data = line.removeprefix("data: ").strip()
                if data == "[DONE]":
                    break
                try:
                    item = json.loads(data)
                except json.JSONDecodeError:
                    continue
                choice = item.get("choices", [{}])[0]
                delta_payload = choice.get("delta") or {}
                message_payload = choice.get("message") or {}
                delta = delta_payload.get("content") or message_payload.get("content")
                if delta:
                    yield str(delta)

    def _chat_payload(
        self,
        question: str,
        hits: list[SearchHit],
        query_type: str,
    ) -> dict[str, object]:
        assert self.settings.chat_model
        evidence = json.dumps(evidence_payload(question, hits, query_type), ensure_ascii=False)
        system = (
            "你是规范与技术文档问答助手。只能依据给定资料回答，禁止使用未提供的常识补充。"
            "数值、单位、否定词和适用条件必须严格核对。综合或版本对比问题必须分别陈述各文档，"
            "明确相同点、变化和适用范围。每个关键结论后用实际资料编号标注，例如 [1]、[2]。"
            "如果资料是表格、长列表或整段条款，先根据用户问题筛选最相关的行、列、单元格或条件，"
            "只总结这些相关内容；不要把整张表、整段资料或不相关行逐项复述到回答里。"
            "回答不要过度压缩：先给直接结论，再补充适用对象、关键数值、条件差异或资料不足之处。"
            "通常用 3 到 6 句回答；复杂对比可以用少量要点，可以指出对应条款。"
            "跨文档对比问题要先判断资料是否直接可比；如果适用对象、条件或条款层级不同，"
            "必须明确说“不能直接判断变严或放宽”，再分别列出各文档规定。"
            "对比回答优先使用：结论、对比要点、资料不足 三段结构。"
            "如果资料包含图片、图示、截面图或图片预览，不要说“无法展示图片”“无法在此呈现”等话；"
            "应直接说明“具体参考资料预览中的图示”或“具体参考对应图示”，并继续归纳图示对应的文字、构造、尺寸和条件。"
            "不要输出独立的参考文献列表、参考资料、引用列表或原文摘录清单。"
            "不要输出资料 JSON、字段名或原文块；最终只输出面向用户的归纳答案。"
        )
        payload: dict[str, object] = {
            "model": self.settings.chat_model,
            "temperature": 0.1,
            "max_tokens": self.settings.chat_max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {
                    "role": "user",
                    "content": f"问题类型：{query_type}\n问题：{question}",
                },
                {
                    "role": "user",
                    "content": (
                        "下面是供你阅读的资料 JSON。它不是输出格式，禁止在回答中复述这些字段：\n"
                        f"{evidence}"
                    ),
                },
            ],
        }
        if self.settings.chat_think is not None:
            payload["think"] = self.settings.chat_think
        return payload

    def _extractive_answer(
        self,
        question: str,
        hits: list[SearchHit],
        query_type: str,
    ) -> str:
        if query_type == "comparison":
            by_document: dict[str, list[tuple[int, SearchHit]]] = {}
            for index, hit in enumerate(hits, 1):
                by_document.setdefault(hit.document_id, []).append((index, hit))
            sections = [
                "当前未配置外部大模型，以下为检索资料的结构化对照。"
                "表格类内容需要接入外部 LLM 后才能按问题抽取相关行并归纳回答："
            ]
            for document_hits in by_document.values():
                first = document_hits[0][1]
                lines = [
                    f"\n### {first.document_title}"
                    f"（{first.standard_no or '编号未识别'}，{first.version or '版本未识别'}）"
                ]
                for index, hit in document_hits[:3]:
                    lines.append(
                        f"- {hit.clause_no or hit.chapter_path or '相关内容'}："
                        f"{evidence_text_for_llm(hit.text)[:220]} [{index}]"
                    )
                sections.extend(lines)
            sections.append("\n请配置外部 LLM 后生成归纳后的变化结论。")
            return "\n".join(sections)
        lines = [
            "当前未配置外部大模型，以下只展示命中的原文资料；"
            "如需按表格行或条件归纳回答，请配置外部 LLM："
        ]
        for index, hit in enumerate(hits[:4], 1):
            lines.append(
                f"- {hit.clause_no or hit.chapter_path or '相关内容'}："
                f"{evidence_text_for_llm(hit.text)[:260]} [{index}]"
            )
        return "\n".join(lines)
