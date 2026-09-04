"""模型服务模块：封装向量化、重排和兼容 OpenAI 协议的回答生成接口。"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import time
from collections.abc import Iterator
from dataclasses import dataclass
from threading import Lock

import httpx

from .config import Settings
from .domain import SearchHit
from .prompt import ANSWER_SYSTEM_PROMPT
from .torch_runtime import prepare_torch_runtime

prepare_torch_runtime()

logger = logging.getLogger("uvicorn.error")

TOKEN_RE = re.compile(r"[A-Za-z0-9]+(?:\.[A-Za-z0-9]+)*|[\u3400-\u9fff]")
ASCII_TOKEN_RE = re.compile(r"[A-Za-z0-9]+(?:\.[A-Za-z0-9]+)*")
HAN_SEQUENCE_RE = re.compile(r"[\u3400-\u9fff]+")
IMAGE_PATH_RE = re.compile(r"images/[^\s\"'<>，。；)）]+", re.IGNORECASE)

QWEN3_RERANK_PREFIX = (
    '<|im_start|>system\nJudge whether the Document meets the requirements based on '
    'the Query and the Instruct provided. Note that the answer can only be "yes" or '
    'no".<|im_end|>\n<|im_start|>user\n'
)
QWEN3_RERANK_SUFFIX = "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"


def lexical_tokens(text: str) -> set[str]:
    tokens = {token.lower() for token in ASCII_TOKEN_RE.findall(text)}
    for sequence in HAN_SEQUENCE_RE.findall(text):
        if len(sequence) == 1:
            tokens.add(sequence)
        else:
            tokens.update(sequence[index : index + 2] for index in range(len(sequence) - 1))
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


def _loads_json_object(content: str) -> dict[str, object] | None:
    text = content.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    else:
        object_match = re.search(r"\{.*\}", text, re.DOTALL)
        if object_match:
            text = object_match.group(0)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


class EmbeddingProvider:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.backend = settings.embedding_backend.strip().lower()
        self._model = None
        self._tokenizer = None
        self._device: str | None = None
        self._model_lock = Lock()

    @property
    def semantic(self) -> bool:
        return (
            self.backend == "local" and self.local_available
            or self.external
            or self.fallback_external
            or self.huggingface_external
        )

    @property
    def external(self) -> bool:
        return self.backend == "openai" and bool(
            self.settings.embedding_api_key and self.settings.embedding_model
        )

    @property
    def fallback_external(self) -> bool:
        return self.settings.embedding_fallback_enabled and bool(
            self.settings.embedding_fallback_api_key and self.settings.embedding_fallback_model
        )

    @property
    def fallback_huggingface(self) -> bool:
        return self.settings.embedding_hf_fallback_enabled and bool(
            self.settings.embedding_hf_api_token and self.settings.embedding_hf_model
        )

    @property
    def huggingface_external(self) -> bool:
        return self.settings.huggingface_fallback_enabled and bool(
            self.settings.huggingface_api_token and self.settings.huggingface_embedding_model
        )

    @property
    def local_available(self) -> bool:
        return self.settings.local_embedding_model_dir.is_dir()

    @property
    def configured(self) -> bool:
        return self.semantic

    def warmup(self) -> None:
        """加载本地模型并完成一次最小推理，避免首个真实请求承担冷启动。"""
        if self.backend == "local" and self.local_available:
            self.embed(["系统启动预热"])

    @property
    def _query_instruction(self) -> str | None:
        model = (self.settings.embedding_model or "").lower()
        if "qwen3-embedding" not in model:
            return None
        return (
            "Instruct: Given a web search query, retrieve relevant passages "
            "that answer the query\nQuery: "
        )

    def embed_query(self, text: str) -> list[float]:
        """检索侧嵌入：Qwen3-Embedding 等非对称模型要求 query 加指令前缀。"""
        instruction = self._query_instruction
        return self.embed([f"{instruction}{text}" if instruction else text])[0]

    def _embed_primary(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if self.external:
            return self._external_embed(texts)
        if self.backend == "local":
            if not self.local_available:
                raise RuntimeError(
                    f"本地 Embedding 模型未下载：{self.settings.local_embedding_model_dir}"
                )
            return self._local_embed(texts)
        return [self._hash_embed(text) for text in texts]

    def embed(self, texts: list[str]) -> list[list[float]]:
        try:
            return self._embed_primary(texts)
        except Exception as exc:
            if self.huggingface_external:
                try:
                    return self._huggingface_embed(texts)
                except (OSError, RuntimeError, ValueError, TypeError) as hf_exc:
                    logger.warning("Hugging Face embedding fallback failed: %s", hf_exc)
            if self.fallback_external:
                logger.warning(
                    "Primary embedding failed; falling back to %s: %s",
                    self.settings.embedding_fallback_model,
                    exc,
                )
                try:
                    return self._fallback_embed(texts)
                except (httpx.HTTPError, KeyError, ValueError, IndexError) as fallback_exc:
                    logger.warning("SiliconFlow embedding fallback failed: %s", fallback_exc)
            if self.fallback_huggingface:
                logger.warning(
                    "Falling back to Hugging Face model %s",
                    self.settings.embedding_hf_model,
                )
                return self._huggingface_embed(texts)
            raise

    def _local_embed(self, texts: list[str]) -> list[list[float]]:
        model, tokenizer, device, torch = self._load_local_model()
        vectors: list[list[float]] = []
        batch_size = max(1, self.settings.embedding_batch_size)
        try:
            for start in range(0, len(texts), batch_size):
                encoded = tokenizer(
                    texts[start : start + batch_size],
                    padding=True,
                    truncation=True,
                    max_length=512,
                    return_tensors="pt",
                )
                encoded = {name: value.to(device) for name, value in encoded.items()}
                with torch.inference_mode():
                    hidden = model(**encoded).last_hidden_state
                    mask = encoded["attention_mask"].unsqueeze(-1).to(hidden.dtype)
                    pooled = (hidden * mask).sum(dim=1) / mask.sum(dim=1).clamp(min=1e-9)
                    normalized = torch.nn.functional.normalize(pooled, p=2, dim=1)
                vectors.extend(normalized.cpu().tolist())
        except Exception:
            if device != "cuda":
                raise
            self._clear_local_model()
            model, tokenizer, device, torch = self._load_local_model(force_device="cpu")
            return self._local_embed(texts)
        if any(len(vector) != self.settings.embedding_dimension for vector in vectors):
            raise ValueError("本地 Embedding 维度与 RAG_EMBEDDING_DIMENSION 不一致")
        return vectors

    def _load_local_model(self, force_device: str | None = None):
        with self._model_lock:
            prepare_torch_runtime()
            import torch
            from transformers import AutoModel, AutoTokenizer

            if self._model is not None and self._tokenizer is not None and self._device:
                return self._model, self._tokenizer, self._device, torch
            configured_device = (force_device or self.settings.local_embedding_device).lower()
            candidates = (
                ["cuda", "cpu"]
                if configured_device == "auto" and torch.cuda.is_available()
                else [configured_device]
            )
            errors: list[str] = []
            for device in candidates:
                try:
                    tokenizer = AutoTokenizer.from_pretrained(
                        self.settings.local_embedding_model_dir,
                        local_files_only=True,
                    )
                    model = AutoModel.from_pretrained(
                        self.settings.local_embedding_model_dir,
                        local_files_only=True,
                    )
                    model.eval()
                    model.to(device)
                    self._model = model
                    self._tokenizer = tokenizer
                    self._device = device
                    return model, tokenizer, device, torch
                except Exception as exc:
                    errors.append(f"{device}: {exc}")
            raise RuntimeError("本地 Embedding 模型加载失败：" + " | ".join(errors))

    def _clear_local_model(self) -> None:
        with self._model_lock:
            self._model = None
            self._tokenizer = None
            self._device = None

    def _external_embed(self, texts: list[str]) -> list[list[float]]:
        assert self.settings.embedding_api_key
        assert self.settings.embedding_model
        response = httpx.post(
            f"{self.settings.embedding_base_url.rstrip('/')}/embeddings",
            headers={"Authorization": f"Bearer {self.settings.embedding_api_key}"},
            json={
                "model": self.settings.embedding_model,
                "input": texts,
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

    def _fallback_embed(self, texts: list[str]) -> list[list[float]]:
        base_url = self.settings.embedding_fallback_base_url.rstrip("/")
        embeddings_url = base_url if base_url.endswith("/embeddings") else f"{base_url}/embeddings"
        response = httpx.post(
            embeddings_url,
            headers={"Authorization": f"Bearer {self.settings.embedding_fallback_api_key}"},
            json={"model": self.settings.embedding_fallback_model, "input": texts},
            timeout=120,
        )
        response.raise_for_status()
        payload = response.json()
        vectors = [
            item["embedding"] for item in sorted(payload["data"], key=lambda item: item["index"])
        ]
        expected = self.settings.embedding_dimension
        if any(len(vector) != expected for vector in vectors):
            actual = len(vectors[0]) if vectors else 0
            raise ValueError(f"后备 Embedding 维度为 {actual}，当前索引要求 {expected}")
        return vectors

    def _huggingface_embed(self, texts: list[str]) -> list[list[float]]:
        from huggingface_hub import InferenceClient

        client = InferenceClient(
            provider=self.settings.embedding_hf_provider,
            api_key=self.settings.embedding_hf_api_token,
        )
        vectors: list[list[float]] = []
        for text in texts:
            result = client.feature_extraction(text, model=self.settings.embedding_hf_model)
            vector = result.tolist() if hasattr(result, "tolist") else result
            if vector and isinstance(vector[0], list):
                vector = vector[0]
            if not isinstance(vector, list) or not all(
                isinstance(value, (int, float)) for value in vector
            ):
                raise ValueError("Hugging Face Embedding 返回格式不是一维向量")
            vectors.append([float(value) for value in vector])
        expected = self.settings.embedding_dimension
        if any(len(vector) != expected for vector in vectors):
            actual = len(vectors[0]) if vectors else 0
            raise ValueError(f"Hugging Face Embedding 维度为 {actual}，当前索引要求 {expected}")
        return vectors

    def _huggingface_embed(self, texts: list[str]) -> list[list[float]]:
        from huggingface_hub import InferenceClient

        client = InferenceClient(
            provider="hf-inference",
            api_key=self.settings.huggingface_api_token,
        )
        vectors: list[list[float]] = []
        for text in texts:
            result = client.feature_extraction(
                text,
                model=self.settings.huggingface_embedding_model,
            )
            vector = result.tolist() if hasattr(result, "tolist") else result
            if vector and isinstance(vector[0], list):
                vector = vector[0]
            if not isinstance(vector, list) or not all(
                isinstance(value, (int, float)) for value in vector
            ):
                raise ValueError("Hugging Face Embedding 返回格式不是一维浮点向量")
            vectors.append([float(value) for value in vector])
        expected = self.settings.embedding_dimension
        if any(len(vector) != expected for vector in vectors):
            actual = len(vectors[0]) if vectors else 0
            raise ValueError(f"Hugging Face Embedding 维度为 {actual}，当前索引要求 {expected}")
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
            except (httpx.HTTPError, KeyError, ValueError, IndexError) as exc:
                logger.warning("Primary rerank failed: %s", exc)
                if self.settings.rerank_fallback_enabled and self.settings.rerank_fallback_api_key:
                    try:
                        return RerankResult(self._fallback_rerank(query, hits, top_n), True)
                    except (httpx.HTTPError, KeyError, ValueError, IndexError) as fallback_exc:
                        logger.warning("Fallback rerank failed: %s", fallback_exc)
                return RerankResult(self._local_rerank(query, hits, top_n), False)
        return RerankResult(self._local_rerank(query, hits, top_n), False)

    def _fallback_rerank(
        self,
        query: str,
        hits: list[SearchHit],
        top_n: int,
    ) -> list[SearchHit]:
        response = httpx.post(
            self.settings.rerank_fallback_url,
            headers={"Authorization": f"Bearer {self.settings.rerank_fallback_api_key}"},
            json={
                "model": self.settings.rerank_fallback_model,
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

    def _qwen3_rerank_texts(
        self, query: str, documents: list[str]
    ) -> tuple[str, list[str]] | None:
        model = (self.settings.rerank_model or "").lower()
        if "qwen3-reranker" not in model:
            return None
        instruction = "Given a web search query, retrieve relevant passages that answer the query"
        formatted_query = (
            f"{QWEN3_RERANK_PREFIX}<Instruct>: {instruction}\n<Query>: {query}\n"
        )
        formatted_documents = [
            f"<Document>: {document}{QWEN3_RERANK_SUFFIX}" for document in documents
        ]
        return formatted_query, formatted_documents

    def _external_rerank(
        self,
        query: str,
        hits: list[SearchHit],
        top_n: int,
    ) -> list[SearchHit]:
        assert self.settings.rerank_base_url
        assert self.settings.rerank_api_key
        assert self.settings.rerank_model
        documents = [hit.text for hit in hits]
        qwen3 = self._qwen3_rerank_texts(query, documents)
        if qwen3:
            query, documents = qwen3
        response = httpx.post(
            self.settings.rerank_base_url,
            headers={"Authorization": f"Bearer {self.settings.rerank_api_key}"},
            json={
                "model": self.settings.rerank_model,
                "query": query,
                "documents": documents,
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

    def _chat_endpoints(self) -> list[tuple[str, str, str]]:
        endpoints: list[tuple[str, str, str]] = []
        if (
            self.settings.openai_base_url
            and self.settings.openai_api_key
            and self.settings.chat_model
        ):
            endpoints.append(
                (
                    self.settings.openai_base_url,
                    self.settings.openai_api_key,
                    self.settings.chat_model,
                )
            )
        if (
            self.settings.fallback_openai_base_url
            and self.settings.fallback_openai_api_key
            and self.settings.fallback_chat_model
        ):
            endpoints.append(
                (
                    self.settings.fallback_openai_base_url,
                    self.settings.fallback_openai_api_key,
                    self.settings.fallback_chat_model,
                )
            )
        return endpoints

    @staticmethod
    def _is_failover_error(error: Exception) -> bool:
        if isinstance(error, httpx.TransportError):
            return True
        if isinstance(error, httpx.HTTPStatusError):
            return error.response.status_code == 429 or error.response.status_code >= 500
        return False

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

    def generate_json(
        self,
        system_prompt: str,
        user_payload: dict[str, object],
        *,
        max_tokens: int = 1200,
    ) -> dict[str, object] | None:
        if not self.external:
            return None
        for endpoint_index, (base_url, api_key, model) in enumerate(self._chat_endpoints()):
            payload: dict[str, object] = {
                "model": model,
                "temperature": 0,
                "max_tokens": max(max_tokens, self.settings.chat_max_tokens),
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
                ],
            }
            if self.settings.chat_think is not None:
                payload["thinking"] = {
                    "type": "enabled" if self.settings.chat_think else "disabled"
                }
            for attempt in range(3):
                try:
                    response = httpx.post(
                        f"{base_url.rstrip('/')}/chat/completions",
                        headers={"Authorization": f"Bearer {api_key}", "Connection": "close"},
                        json=payload,
                        timeout=180,
                    )
                    response.raise_for_status()
                    message = response.json()["choices"][0]["message"]
                    content = message.get("content") or ""
                    if not content:
                        logger.warning("structured_generation_empty_content model=%s", model)
                    parsed = _loads_json_object(str(content))
                    if parsed is None:
                        logger.warning("structured_generation_json_invalid model=%s", model)
                    return parsed
                except httpx.TransportError as exc:
                    if attempt < 2:
                        logger.warning(
                            "structured_generation_retry model=%s attempt=%s error=%s",
                            model,
                            attempt + 1,
                            exc,
                        )
                        time.sleep(attempt + 1)
                        continue
                    error: Exception = exc
                except httpx.HTTPStatusError as exc:
                    error = exc
                except (KeyError, TypeError) as exc:
                    logger.warning(
                        "structured_generation_response_invalid model=%s error=%s", model, exc
                    )
                    return None
                if endpoint_index + 1 >= len(
                    self._chat_endpoints()
                ) or not self._is_failover_error(error):
                    logger.warning(
                        "structured_generation_request_failed model=%s error=%s", model, error
                    )
                    return None
                logger.warning(
                    "structured_generation_primary_failed_using_fallback error=%s", error
                )
                break
        return None

    def _external_answer(
        self,
        question: str,
        hits: list[SearchHit],
        query_type: str,
    ) -> str:
        last_error: Exception | None = None
        endpoints = self._chat_endpoints()
        for index, (base_url, api_key, model) in enumerate(endpoints):
            payload = self._chat_payload(question, hits, query_type)
            payload["model"] = model
            try:
                response = httpx.post(
                    f"{base_url.rstrip('/')}/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}"},
                    json=payload,
                    timeout=180,
                )
                response.raise_for_status()
                return response.json()["choices"][0]["message"]["content"].strip()
            except Exception as exc:
                last_error = exc
                if index + 1 >= len(endpoints) or not self._is_failover_error(exc):
                    raise
                logger.warning("chat_primary_failed_using_fallback error=%s", exc)
        assert last_error is not None
        raise last_error

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
        last_error: Exception | None = None
        endpoints = self._chat_endpoints()
        for index, (base_url, api_key, model) in enumerate(endpoints):
            emitted = False
            payload = self._chat_payload(question, hits, query_type)
            payload["model"] = model
            payload["stream"] = True
            try:
                with httpx.stream(
                    "POST",
                    f"{base_url.rstrip('/')}/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}"},
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
                            emitted = True
                            yield str(delta)
                return
            except Exception as exc:
                last_error = exc
                if emitted or index + 1 >= len(endpoints) or not self._is_failover_error(exc):
                    raise
                logger.warning("chat_stream_primary_failed_using_fallback error=%s", exc)
        assert last_error is not None
        raise last_error

    def _chat_payload(
        self,
        question: str,
        hits: list[SearchHit],
        query_type: str,
    ) -> dict[str, object]:
        assert self.settings.chat_model
        evidence = json.dumps(evidence_payload(question, hits, query_type), ensure_ascii=False)
        system = ANSWER_SYSTEM_PROMPT
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
            payload["thinking"] = {"type": "enabled" if self.settings.chat_think else "disabled"}
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
