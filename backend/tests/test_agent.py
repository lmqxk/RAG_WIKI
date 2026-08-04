import time
from threading import Lock

from backend.agent import AgenticRetriever, AgentStep, _loads_json_object
from backend.config import Settings
from backend.domain import SearchHit


def make_hit(chunk_id: str, document_id: str, score: float = 0.8) -> SearchHit:
    return SearchHit(
        chunk_id=chunk_id,
        document_id=document_id,
        text=f"{document_id} 工业建筑 防火要求",
        clause_no=None,
        chapter_path="",
        page_start=1,
        page_end=1,
        printed_page=None,
        document_title=document_id,
        standard_no=document_id,
        version=None,
        score=score,
        source="test",
        content_type="normative",
    )


class FakeRepository:
    def list_documents(self) -> list[dict[str, object]]:
        return [
            {
                "id": "new",
                "title": "新规范",
                "filename": "GB55037-2022.pdf",
                "standard_no": "GB55037-2022",
                "version": "2022",
                "status": "READY",
            },
            {
                "id": "old",
                "title": "旧规范",
                "filename": "GB50016-2014.pdf",
                "standard_no": "GB50016-2014",
                "version": "2018",
                "status": "READY",
            },
        ]


class FakeReranker:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int, int]] = []

    def rerank(self, query: str, hits: list[SearchHit], top_n: int):
        self.calls.append((query, len(hits), top_n))

        class Result:
            def __init__(self, ranked: list[SearchHit]) -> None:
                self.hits = ranked

        return Result(sorted(hits, key=lambda hit: hit.score, reverse=True)[:top_n])


class FakeHybridRetriever:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[str, ...] | None]] = []
        self.lock = Lock()
        self.reranker = FakeReranker()

    def retrieve(
        self,
        question: str,
        document_ids: list[str] | None,
    ) -> tuple[str, list[SearchHit]]:
        with self.lock:
            self.calls.append((question, tuple(document_ids) if document_ids else None))
        return "fact", [make_hit("base", "new")]

    def _retrieve_scope(
        self,
        question: str,
        document_ids: list[str] | None,
        *,
        rerank: bool = True,
    ) -> list[SearchHit]:
        scope = tuple(document_ids) if document_ids else None
        with self.lock:
            self.calls.append((question, scope))
            old_call_count = len([call for call in self.calls if call[1] == ("old",)])
        assert rerank is False
        if scope == ("new",):
            return [make_hit("new-1", "new", 0.9)]
        if scope == ("old",) and old_call_count > 1:
            return [make_hit("old-1", "old", 0.88)]
        return []

    def _balance_documents(self, hits: list[SearchHit]) -> list[SearchHit]:
        return hits


class SlowHybridRetriever(FakeHybridRetriever):
    def _retrieve_scope(
        self,
        question: str,
        document_ids: list[str] | None,
        *,
        rerank: bool = True,
    ) -> list[SearchHit]:
        time.sleep(0.2)
        return super()._retrieve_scope(question, document_ids, rerank=rerank)


def test_agentic_retriever_supplements_missing_comparison_document() -> None:
    retriever = FakeHybridRetriever()
    agent = AgenticRetriever(
        Settings(
            agentic_max_steps=6,
            agentic_min_hits=2,
            agentic_planner_llm_enabled=False,
        ),
        FakeRepository(),  # type: ignore[arg-type]
        retriever,  # type: ignore[arg-type]
    )

    result = agent.run("对比 GB55037-2022 和 GB50016-2014 的工业建筑防火要求", None)

    assert result.kind == "comparison"
    assert result.supplemented is True
    assert {hit.document_id for hit in result.hits} == {"new", "old"}
    assert len(retriever.calls) > 2
    assert len(retriever.reranker.calls) == 1


def test_agentic_retriever_executes_steps_in_parallel() -> None:
    retriever = SlowHybridRetriever()
    agent = AgenticRetriever(
        Settings(agentic_parallel_workers=2),
        FakeRepository(),  # type: ignore[arg-type]
        retriever,  # type: ignore[arg-type]
    )
    started_at = time.perf_counter()

    hits = agent._execute_steps(
        [
            AgentStep("search_in_document", "query 1", ["new"], "test"),
            AgentStep("search_in_document", "query 2", ["new"], "test"),
        ]
    )

    assert len(hits) == 2
    assert time.perf_counter() - started_at < 0.35


def test_agentic_retriever_can_delegate_to_fixed_pipeline() -> None:
    retriever = FakeHybridRetriever()
    agent = AgenticRetriever(
        Settings(agentic_retrieval_enabled=False),
        FakeRepository(),  # type: ignore[arg-type]
        retriever,  # type: ignore[arg-type]
    )

    kind, hits = agent.retrieve("普通问题", ["new"])

    assert kind == "fact"
    assert [hit.chunk_id for hit in hits] == ["base"]
    assert retriever.calls == [("普通问题", ("new",))]


class FakeResponse:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return {
            "choices": [
                {
                    "message": {
                        "content": (
                            '{"intent": "comparison", '
                            '"query_rewrites": ["新规范 防火分区", "旧规范 防火分区"], '
                            '"steps": ['
                            '{"tool": "search_in_document", "query": "新规范 防火分区", '
                            '"document_ref": "GB55037-2022", "dimension": "新规范", '
                            '"reason": "查新规范"},'
                            '{"tool": "search_in_document", "query": "旧规范 防火分区", '
                            '"document_ref": "GB50016-2014", "dimension": "旧规范", '
                            '"reason": "查旧规范"}'
                            "]}"
                        )
                    }
                }
            ]
        }


class InvalidToolResponse:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return {
            "choices": [
                {
                    "message": {
                        "content": (
                            '{"intent": "exploration", '
                            '"query_rewrites": ["非法外部搜索"], '
                            '"steps": ['
                            '{"tool": "web_search", "query": "非法工具", '
                            '"dimension": "外部搜索"}'
                            "]}"
                        )
                    }
                }
            ]
        }


def test_agentic_retriever_uses_llm_planner_json(monkeypatch) -> None:
    retriever = FakeHybridRetriever()
    monkeypatch.setattr("backend.agent.httpx.post", lambda *args, **kwargs: FakeResponse())
    agent = AgenticRetriever(
        Settings(openai_api_key="key", chat_model="planner", agentic_min_hits=1),
        FakeRepository(),  # type: ignore[arg-type]
        retriever,  # type: ignore[arg-type]
    )

    result = agent.run("对比新旧规范防火分区", None)

    assert result.kind == "comparison"
    assert ("新规范 防火分区", ("new",)) in retriever.calls
    assert ("旧规范 防火分区", ("old",)) in retriever.calls


def test_agentic_retriever_falls_back_when_llm_tool_is_invalid(monkeypatch) -> None:
    retriever = FakeHybridRetriever()
    monkeypatch.setattr("backend.agent.httpx.post", lambda *args, **kwargs: InvalidToolResponse())
    agent = AgenticRetriever(
        Settings(openai_api_key="key", chat_model="planner", agentic_min_hits=1),
        FakeRepository(),  # type: ignore[arg-type]
        retriever,  # type: ignore[arg-type]
    )

    agent.run("普通问题", ["new"])

    assert all(call[0] != "非法工具" for call in retriever.calls)
    assert retriever.calls[0][1] == ("new",)


def test_loads_json_object_accepts_markdown_fence() -> None:
    assert _loads_json_object('```json\n{"steps": []}\n```') == {"steps": []}
