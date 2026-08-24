"""向量索引模块：管理 Qdrant 集合、文本向量写入、删除与相似度查询。

支持两种模式：
- 本地（默认）：使用 Qdrant Local (path)，数据存储在磁盘上
- 远程（设置 RAG_QDRANT_URL 时）：使用 Qdrant Server。支持 API Key 认证
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from qdrant_client import QdrantClient, models

from .config import Settings
from .domain import Chunk
from .providers import EmbeddingProvider


@dataclass(slots=True)
class DocumentVectorSnapshot:
    """文档替换前的向量快照，用于 SQLite 写入失败后的补偿恢复。"""

    points: list[models.PointStruct]


class VectorIndex:
    def __init__(self, settings: Settings, embeddings: EmbeddingProvider) -> None:
        self.settings = settings
        self.embeddings = embeddings
        self.collection_name = settings.embedding_collection_name

        if settings.qdrant_is_remote:
            self.client = QdrantClient(
                url=settings.qdrant_url,
                api_key=settings.qdrant_api_key,
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

    def replace_document(
        self,
        document_id: str,
        chunks: Sequence[Chunk],
        organization_id: str | None = None,
    ) -> DocumentVectorSnapshot:
        """替换一个文档的向量，并在写入失败时恢复旧向量。"""

        snapshot = self.snapshot_document(document_id)
        try:
            self._delete_document(document_id)
            self._upsert_chunks(chunks, organization_id=organization_id)
        except Exception:
            self.restore_document(document_id, snapshot)
            raise
        return snapshot

    def snapshot_document(self, document_id: str) -> DocumentVectorSnapshot:
        """读取文档全部向量，避免批量替换失败时丢失已可用索引。"""

        points: list[models.PointStruct] = []
        offset: int | str | None = None
        while True:
            records, offset = self.client.scroll(
                collection_name=self.collection_name,
                scroll_filter=self._document_filter(document_id),
                limit=256,
                offset=offset,
                with_payload=True,
                with_vectors=True,
            )
            points.extend(
                models.PointStruct(
                    id=record.id,
                    vector=record.vector,
                    payload=record.payload,
                )
                for record in records
                if record.vector is not None
            )
            if offset is None:
                break
        return DocumentVectorSnapshot(points=points)

    def restore_document(self, document_id: str, snapshot: DocumentVectorSnapshot) -> None:
        """删除当前文档向量后恢复一个已知可用的快照。"""

        self._delete_document(document_id)
        self._upsert_points(snapshot.points)

    def _delete_document(self, document_id: str) -> None:
        self.client.delete(
            collection_name=self.collection_name,
            points_selector=models.FilterSelector(filter=self._document_filter(document_id)),
            wait=True,
        )

    def _upsert_chunks(
        self,
        chunks: Sequence[Chunk],
        organization_id: str | None = None,
    ) -> None:
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
            points = []
            for chunk, vector in zip(batch, vectors, strict=True):
                payload: dict[str, object] = {"document_id": chunk.document_id}
                if organization_id is not None:
                    payload["organization_id"] = organization_id
                points.append(
                    models.PointStruct(
                        id=chunk.id,
                        vector=vector,
                        payload=payload,
                    )
                )
            self.client.upsert(
                collection_name=self.collection_name,
                points=points,
                wait=True,
            )

    def _upsert_points(self, points: Sequence[models.PointStruct]) -> None:
        batch_size = 32
        for start in range(0, len(points), batch_size):
            self.client.upsert(
                collection_name=self.collection_name,
                points=list(points[start : start + batch_size]),
                wait=True,
            )

    @staticmethod
    def _document_filter(
        document_id: str,
        organization_id: str | None = None,
    ) -> models.Filter:
        conditions: list[models.Condition] = [
            models.FieldCondition(
                key="document_id",
                match=models.MatchValue(value=document_id),
            )
        ]
        if organization_id is not None:
            conditions.append(
                models.FieldCondition(
                    key="organization_id",
                    match=models.MatchValue(value=organization_id),
                )
            )
        return models.Filter(must=conditions)

    def search(
        self,
        query: str,
        *,
        document_ids: Sequence[str] | None,
        organization_id: str | None = None,
        limit: int,
    ) -> list[tuple[str, float]]:
        query_filter = None
        must_conditions: list[models.Condition] = []
        if document_ids:
            must_conditions.append(
                models.FieldCondition(
                    key="document_id",
                    match=models.MatchAny(any=list(document_ids)),
                )
            )
        if organization_id is not None:
            must_conditions.append(
                models.FieldCondition(
                    key="organization_id",
                    match=models.MatchValue(value=organization_id),
                )
            )
        if must_conditions:
            query_filter = models.Filter(must=must_conditions)
        response = self.client.query_points(
            collection_name=self.collection_name,
            query=self.embeddings.embed([query])[0],
            query_filter=query_filter,
            limit=limit,
            with_payload=False,
        )
        return [(str(point.id), float(point.score)) for point in response.points]
