"""验证文档重新解析服务：替换已上传 PDF，并重新提交解析任务。"""

import hashlib
from pathlib import Path

import pytest

from backend.config import Settings
from backend.db import Database
from backend.domain import SearchHit
from backend.agent import AgentRun
from backend.repository import Repository, summarize_error
from backend.service import RagService, preview_from_parsed


class FakeUpload:
    def __init__(self, filename: str, content: bytes) -> None:
        self.filename = filename
        self._content = content
        self._offset = 0

    async def read(self, size: int = -1) -> bytes:
        if self._offset >= len(self._content):
            return b""
        if size < 0:
            size = len(self._content) - self._offset
        chunk = self._content[self._offset : self._offset + size]
        self._offset += len(chunk)
        return chunk


class FakeIngestion:
    def __init__(self) -> None:
        self.submitted: list[str] = []

    def submit(self, job_id: str) -> None:
        self.submitted.append(job_id)


class FakeRetriever:
    def run(
        self,
        question: str,
        document_ids: list[str] | None,
    ) -> AgentRun:
        return AgentRun(
            kind="fact",
            hits=[
                SearchHit(
                    chunk_id="chunk-1",
                    document_id="doc-1",
                    text="表格内容：A 行命中，B 行不相关",
                    clause_no="5.2.1",
                    chapter_path="5 章节",
                    page_start=12,
                    page_end=12,
                    printed_page=None,
                    document_title="测试规范",
                    standard_no="GB 1",
                    version="2024",
                    score=0.9,
                    source="test",
                    content_type="normative_table",
                )
            ],
            steps=[],
            supplemented=False,
            planner_ms=10.0,
            recall_ms=20.0,
            rerank_ms=30.0,
        )


class FakeChatProvider:
    external = True

    def answer(
        self,
        question: str,
        hits: list[SearchHit],
        query_type: str,
    ) -> str:
        return "只回答最相关的 A 行。[1]"


def make_service(tmp_path: Path) -> tuple[RagService, Repository, FakeIngestion]:
    database = Database(tmp_path / "rag.db")
    database.initialize()
    repository = Repository(database)
    ingestion = FakeIngestion()
    service = RagService(
        Settings(data_dir=tmp_path, mineru_model_dir=tmp_path / "models"),
        repository,
        ingestion,  # type: ignore[arg-type]
        retriever=None,  # type: ignore[arg-type]
        chat_provider=None,  # type: ignore[arg-type]
    )
    return service, repository, ingestion


def test_chat_keeps_citations_structured_without_appending_reference_text(tmp_path: Path) -> None:
    database = Database(tmp_path / "rag.db")
    database.initialize()
    repository = Repository(database)
    service = RagService(
        Settings(data_dir=tmp_path, mineru_model_dir=tmp_path / "models"),
        repository,
        ingestion=FakeIngestion(),  # type: ignore[arg-type]
        retriever=FakeRetriever(),  # type: ignore[arg-type]
        chat_provider=FakeChatProvider(),  # type: ignore[arg-type]
    )

    result = service.chat("A 行怎么规定？", ["doc-1"])

    assert result.answer == "只回答最相关的 A 行。[1]"
    assert "引用：" not in result.answer
    assert len(result.citations) == 1


def test_summarize_error_hides_cuda_traceback() -> None:
    error = (
        "OpenDataLab PDF-Extract-Kit 1.0 解析失败（退出码 1）：\n"
        "Traceback...\n"
        "torch.AcceleratorError: CUDA error: no kernel image is available for "
        "execution on the device\n"
        "File \"x\", line 1"
    )

    summary = summarize_error(error)

    assert summary is not None
    assert "CUDA 与当前 PyTorch wheel 不兼容" in summary
    assert "Traceback" not in summary
    assert "Qwen2VisionTransformerPretrainedModel" not in summary


def test_preview_from_parsed_uses_table_image(tmp_path: Path) -> None:
    parsed_dir = tmp_path / "parsed" / "doc-1"
    auto_dir = parsed_dir / "doc-1" / "auto"
    image_dir = auto_dir / "images"
    image_dir.mkdir(parents=True)
    (image_dir / "table.jpg").write_bytes(b"fake")
    (auto_dir / "doc-1_content_list.json").write_text(
        '[{"type":"table","page_idx":0,"img_path":"images/table.jpg","table_body":"<table></table>"}]',
        encoding="utf-8",
    )
    document = {"id": "doc-1", "parsed_path": str(parsed_dir)}

    url, label = preview_from_parsed(document, 1, "normative_table")

    assert label == "表格截图"
    assert url == "/api/documents/doc-1/asset?path=doc-1/auto/images/table.jpg"


def test_preview_from_parsed_uses_normalized_image_context(tmp_path: Path) -> None:
    parsed_dir = tmp_path / "parsed" / "doc-1"
    auto_dir = parsed_dir / "doc-1" / "auto"
    image_dir = auto_dir / "images"
    image_dir.mkdir(parents=True)
    (image_dir / "c0fb.jpg").write_bytes(b"fake")
    (image_dir / "f078.jpg").write_bytes(b"fake")
    (auto_dir / "doc-1_content_list.json").write_text("[]", encoding="utf-8")
    (parsed_dir / "normalized.json").write_text(
        """
        {
          "blocks": [
            {
              "page": 2,
              "type": "table",
              "images": [
                {
                  "path": "images/c0fb.jpg",
                  "caption": "橡檩屋顶截面",
                  "row_context": "屋顶承重构件",
                  "column_context": "截面图和结构厚度或截面最小尺寸(mm)",
                  "cell_text": "橡檩屋顶截面0.50轻型木桁架屋顶截面",
                  "order": 1
                },
                {
                  "path": "images/f078.jpg",
                  "caption": "轻型木桁架屋顶截面",
                  "row_context": "屋顶承重构件",
                  "column_context": "截面图和结构厚度或截面最小尺寸(mm)",
                  "cell_text": "橡檩屋顶截面0.50轻型木桁架屋顶截面",
                  "order": 2
                }
              ]
            }
          ]
        }
        """,
        encoding="utf-8",
    )
    document = {"id": "doc-1", "parsed_path": str(parsed_dir)}

    url, label = preview_from_parsed(
        document,
        2,
        "normative_table",
        "屋顶承重构件包含两张截面图",
        "轻型木桁架屋顶截面图",
    )

    assert label
    assert url == "/api/documents/doc-1/asset?path=doc-1/auto/images/f078.jpg"


@pytest.mark.asyncio
async def test_reparse_document_replaces_file_and_submits_job(tmp_path: Path) -> None:
    service, repository, ingestion = make_service(tmp_path)
    stored_path = tmp_path / "uploads" / "doc-1.pdf"
    stored_path.parent.mkdir(parents=True)
    stored_path.write_bytes(b"old pdf")
    repository.create_document(
        document_id="doc-1",
        filename="old.pdf",
        stored_path=stored_path,
        sha256="old-sha",
        title="old",
        standard_no=None,
        version=None,
        file_size=7,
    )
    repository.update_document(
        "doc-1",
        status="READY",
        page_count=9,
        needs_ocr=1,
        parser_name="mineru",
        parsed_path=str(tmp_path / "parsed" / "doc-1"),
        error="old error",
    )

    document, job_id = await service.reparse_document(
        "doc-1",
        FakeUpload("new.pdf", b"%PDF-new"),
    )

    assert stored_path.read_bytes() == b"%PDF-new"
    assert document["filename"] == "new.pdf"
    assert document["status"] == "QUEUED"
    assert document["page_count"] == 0
    assert document["needs_ocr"] is False
    assert document["parser_name"] is None
    assert document["parsed_path"] is None
    assert document["error"] is None
    assert repository.get_job(job_id) is not None
    assert ingestion.submitted == [job_id]


@pytest.mark.asyncio
async def test_upload_duplicate_failed_document_resubmits_job(tmp_path: Path) -> None:
    service, repository, ingestion = make_service(tmp_path)
    stored_path = tmp_path / "uploads" / "doc-1.pdf"
    stored_path.parent.mkdir(parents=True)
    content = b"%PDF-duplicate"
    stored_path.write_bytes(content)
    sha256 = hashlib.sha256(content).hexdigest()
    repository.create_document(
        document_id="doc-1",
        filename="old.pdf",
        stored_path=stored_path,
        sha256=sha256,
        title="old",
        standard_no=None,
        version=None,
        file_size=len(content),
    )
    failed_job_id = "failed-job"
    repository.create_job(failed_job_id, "doc-1")
    repository.update_document("doc-1", status="FAILED", error="old failure")
    repository.update_job(failed_job_id, status="FAILED", stage="failed", error="old failure")

    document, job_id, duplicate = await service.upload(FakeUpload("old.pdf", content))

    assert duplicate is True
    assert job_id != failed_job_id
    assert document["status"] == "QUEUED"
    assert document["error"] is None
    assert repository.get_job(job_id) is not None
    assert ingestion.submitted[-1] == job_id


@pytest.mark.asyncio
async def test_reparse_document_rejects_active_job(tmp_path: Path) -> None:
    service, repository, _ingestion = make_service(tmp_path)
    stored_path = tmp_path / "uploads" / "doc-1.pdf"
    stored_path.parent.mkdir(parents=True)
    stored_path.write_bytes(b"old pdf")
    repository.create_document(
        document_id="doc-1",
        filename="old.pdf",
        stored_path=stored_path,
        sha256="old-sha",
        title="old",
        standard_no=None,
        version=None,
        file_size=7,
    )
    repository.create_job("job-1", "doc-1")

    with pytest.raises(RuntimeError):
        await service.reparse_document("doc-1", FakeUpload("new.pdf", b"%PDF-new"))
