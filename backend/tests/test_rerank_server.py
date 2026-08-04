"""验证本地重排序服务：确保接口兼容主 RAG 的 RerankProvider。"""

from backend.rerank_server import RerankRequest, rerank


class FakeModel:
    def rerank(self, query: str, documents: list[str], top_n: int | None = None):
        assert query == "防火间距"
        ranked = [
            {"index": 1, "relevance_score": 0.91, "document": documents[1]},
            {"index": 0, "relevance_score": 0.52, "document": documents[0]},
        ]
        return ranked[:top_n]


def test_rerank_endpoint_returns_jina_compatible_results(monkeypatch) -> None:
    monkeypatch.setattr("backend.rerank_server.load_model", lambda: FakeModel())

    response = rerank(
        RerankRequest(
            model="jina-reranker-v3.5",
            query="防火间距",
            documents=["无关内容", "防火间距不应小于50m"],
            top_n=1,
            return_documents=False,
        )
    )

    assert response.model == "jina-reranker-v3.5"
    assert response.results == [{"index": 1, "relevance_score": 0.91}]
