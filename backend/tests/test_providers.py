"""验证模型资料包：确保跨文档对比按文档分组传给 LLM。"""

import httpx

from backend.config import Settings
from backend.domain import SearchHit
from backend.providers import ChatProvider, EmbeddingProvider, RerankProvider, evidence_payload


def make_hit(chunk_id: str, document_id: str, standard_no: str) -> SearchHit:
    return SearchHit(
        chunk_id=chunk_id,
        document_id=document_id,
        text=f"{standard_no} 防火间距要求",
        clause_no="3.1.1",
        chapter_path="3 总平面布局",
        page_start=1,
        page_end=1,
        printed_page=None,
        document_title=f"{standard_no} 规范",
        standard_no=standard_no,
        version=standard_no.split("-")[-1],
        score=1.0,
        source="test",
        content_type="normative",
    )


def test_comparison_evidence_payload_groups_hits_by_document() -> None:
    payload = evidence_payload(
        "对比新旧规范防火间距",
        [
            make_hit("new-1", "new", "GB55037-2022"),
            make_hit("old-1", "old", "GB50016-2014"),
            make_hit("new-2", "new", "GB55037-2022"),
        ],
        "comparison",
    )

    assert isinstance(payload, dict)
    documents = payload["documents"]
    assert isinstance(documents, list)
    assert [document["document_id"] for document in documents] == ["new", "old"]
    assert [item["id"] for item in documents[0]["items"]] == [1, 3]
    assert [item["id"] for item in documents[1]["items"]] == [2]


def test_rerank_provider_falls_back_when_external_service_fails(monkeypatch) -> None:
    provider = RerankProvider(
        Settings(
            rerank_base_url="http://127.0.0.1:8011/rerank",
            rerank_api_key="local",
            rerank_model="jina-reranker-v3.5",
        )
    )
    hits = [
        make_hit("a", "doc", "GB50016-2014"),
        make_hit("b", "doc", "GB50016-2014"),
    ]
    hits[0].text = "篮球运动规则"
    hits[1].text = "防火间距不应小于50m"

    def fail(*args, **kwargs):
        raise httpx.HTTPStatusError(
            "bad gateway",
            request=httpx.Request("POST", "http://127.0.0.1:8011/rerank"),
            response=httpx.Response(502),
        )

    monkeypatch.setattr(provider, "_external_rerank", fail)

    result = provider.rerank("防火间距", hits, 1)

    assert result.external is False
    assert result.hits[0].chunk_id == "b"


def test_chat_provider_stream_uses_extractive_answer_without_external_llm() -> None:
    provider = ChatProvider(Settings(openai_api_key=None, chat_model=None))
    chunks = list(
        provider.answer_stream(
            "防火间距",
            [make_hit("a", "doc", "GB50016-2014")],
            "fact",
        )
    )

    assert len(chunks) == 1
    assert "GB50016-2014" in chunks[0]


def test_local_embedding_is_not_configured_until_model_is_downloaded(tmp_path) -> None:
    provider = EmbeddingProvider(
        Settings(local_embedding_model_dir=tmp_path / "missing-bge-model")
    )

    assert provider.backend == "local"
    assert provider.configured is False


def test_chat_provider_stream_ignores_reasoning_content(monkeypatch) -> None:
    provider = ChatProvider(
        Settings(openai_api_key="key", chat_model="chat", openai_base_url="http://llm")
    )

    class FakeStreamResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback) -> None:
            return None

        def raise_for_status(self) -> None:
            return None

        def iter_lines(self):
            yield 'data: {"choices":[{"delta":{"reasoning_content":"不要输出思考"}}]}'
            yield 'data: {"choices":[{"delta":{"content":"正式答案"}}]}'
            yield "data: [DONE]"

    monkeypatch.setattr(
        "backend.providers.httpx.stream",
        lambda *args, **kwargs: FakeStreamResponse(),
    )

    chunks = list(provider.answer_stream("问题", [make_hit("a", "doc", "GB50016-2014")], "fact"))

    assert chunks == ["正式答案"]
