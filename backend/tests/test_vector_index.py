"""验证 Qdrant 文档向量替换失败时会恢复旧快照。"""

from types import SimpleNamespace

import pytest

from backend.domain import Chunk
from backend.vector_index import VectorIndex


class FakeEmbeddings:
    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[float(index)] for index, _text in enumerate(texts, 1)]


class FakeQdrant:
    def __init__(self, *, fail_upsert: bool = False) -> None:
        self.fail_upsert = fail_upsert
        self.points = {
            "old-chunk": SimpleNamespace(
                id="old-chunk",
                vector=[0.5],
                payload={"document_id": "doc-1"},
            )
        }

    def scroll(self, **_kwargs):
        return list(self.points.values()), None

    def delete(self, **_kwargs) -> None:
        self.points.clear()

    def upsert(self, *, points, **_kwargs) -> None:
        if self.fail_upsert and any(str(point.id).startswith("new-") for point in points):
            raise RuntimeError("simulated qdrant write failure")
        self.points.update(
            {
                str(point.id): SimpleNamespace(
                    id=point.id,
                    vector=point.vector,
                    payload=point.payload,
                )
                for point in points
            }
        )


def make_index(client: FakeQdrant) -> VectorIndex:
    index = VectorIndex.__new__(VectorIndex)
    index.client = client
    index.collection_name = "test"
    index.embeddings = FakeEmbeddings()
    return index


def test_replace_document_restores_existing_vectors_when_qdrant_write_fails() -> None:
    client = FakeQdrant(fail_upsert=True)
    index = make_index(client)
    chunk = Chunk(
        id="new-chunk",
        document_id="doc-1",
        ordinal=0,
        chapter_path="第 1 章",
        clause_no="1.0.1",
        text="新条文",
        page_start=1,
        page_end=1,
    )

    with pytest.raises(RuntimeError, match="simulated qdrant write failure"):
        index.replace_document("doc-1", [chunk])

    assert list(client.points) == ["old-chunk"]
