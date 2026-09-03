"""向量索引模块：管理 Qdrant 本地集合、文本向量写入、删除与相似度查询。"""

from __future__ import annotations

from collections.abc import Sequence
from ipaddress import ip_address
from urllib.parse import urlparse

from qdrant_client import QdrantClient, models

from .config import Settings
from .domain import Chunk
from .providers import EmbeddingProvider


def _should_bypass_proxy(url: str | None) -> bool:
    if not url:
        return False
    hostname = urlparse(url).hostname
    if hostname in {"localhost", "127.0.0.1", "::1"}:
        return True
    if not hostname:
        return False
    try:
        address = ip_address(hostname)
    except ValueError:
        return False
    return address.is_private or address.is_loopback


class VectorIndex:
    def __init__(self, settings: Settings, embeddings: EmbeddingProvider) -> None:
        self.settings = settings
        self.embeddings = embeddings
        self.collection_name = settings.embedding_collection_name
        if settings.qdrant_is_remote:
            self.client = QdrantClient(
                url=settings.qdrant_url,
                api_key=settings.qdrant_api_key,
                trust_env=not _should_bypass_proxy(settings.qdrant_url),
            )
        else:
            self.client = QdrantClient(path=str(settings.qdrant_path))
        if not self.client.collection_exists(self.collection_name):
            self.client.create_collection(
                collection_name=self.collection_name,
                vectors_config=models.VectorParams(
                    size=settings.embedding_dimension,
                    distance=models.Distance.COSINE,
                ),
            )

    def replace_document(self, document_id: str, chunks: Sequence[Chunk]) -> None:
        self.client.delete(
            collection_name=self.collection_name,
            points_selector=models.FilterSelector(
                filter=models.Filter(
                    must=[
                        models.FieldCondition(
                            key="document_id",
                            match=models.MatchValue(value=document_id),
                        )
                    ]
                )
            ),
            wait=True,
        )
        batch_size = 32
        for start in range(0, len(chunks), batch_size):
            batch = list(chunks[start : start + batch_size])
            vectors = self.embeddings.embed(
                [
                    " ".join(
                        [
                            chunk.chapter_path,
                            chunk.clause_no or "",
                            chunk.text,
                        ]
                    )
                    for chunk in batch
                ]
            )
            self.client.upsert(
                collection_name=self.collection_name,
                points=[
                    models.PointStruct(
                        id=chunk.id,
                        vector=vector,
                        payload={"document_id": chunk.document_id},
                    )
                    for chunk, vector in zip(batch, vectors, strict=True)
                ],
                wait=True,
            )

    def search(
        self,
        query: str,
        *,
        document_ids: Sequence[str] | None,
        limit: int,
    ) -> list[tuple[str, float]]:
        query_filter = None
        if document_ids:
            query_filter = models.Filter(
                must=[
                    models.FieldCondition(
                        key="document_id",
                        match=models.MatchAny(any=list(document_ids)),
                    )
                ]
            )
        response = self.client.query_points(
            collection_name=self.collection_name,
            query=self.embeddings.embed_query(query),
            query_filter=query_filter,
            limit=limit,
            with_payload=False,
        )
        return [(str(point.id), float(point.score)) for point in response.points]
