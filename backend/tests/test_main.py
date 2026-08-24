"""验证 API 层文件响应行为。"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.db import Database
from backend.main import OrganizationContext, favicon, get_document_file, reindex_document
from backend.repository import Repository


def test_document_file_response_is_inline_preview(tmp_path: Path) -> None:
    database = Database(tmp_path / "rag.db")
    database.initialize()
    repository = Repository(database)
    pdf_path = tmp_path / "uploads" / "doc-1.pdf"
    pdf_path.parent.mkdir(parents=True)
    pdf_path.write_bytes(b"%PDF-1.4")
    repository.create_document(
        document_id="doc-1",
        filename="test.pdf",
        stored_path=pdf_path,
        sha256="abc",
        title="test",
        standard_no=None,
        version=None,
        file_size=8,
    )
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repository=repository)))
    org = OrganizationContext(organization_id="default-org", user_id="", role="admin")

    response = get_document_file("doc-1", request, org)  # type: ignore[arg-type]

    assert response.media_type == "application/pdf"
    assert response.headers["content-disposition"].startswith("inline;")


def test_favicon_response_uses_frontend_svg() -> None:
    response = favicon()

    assert response.media_type == "image/svg+xml"


class FakeIngestion:
    def __init__(self) -> None:
        self.submitted: list[str] = []

    def submit_reindex(self, job_id: str) -> None:
        self.submitted.append(job_id)


def test_reindex_rejects_document_with_active_job(tmp_path: Path) -> None:
    database = Database(tmp_path / "rag.db")
    database.initialize()
    repository = Repository(database)
    repository.create_document(
        document_id="doc-1",
        filename="test.pdf",
        stored_path=tmp_path / "test.pdf",
        sha256="abc",
        title="test",
        standard_no=None,
        version=None,
        file_size=8,
    )
    repository.create_job("existing-job", "doc-1")
    ingestion = FakeIngestion()
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(repository=repository, ingestion=ingestion))
    )
    org = OrganizationContext(organization_id="default-org", user_id="", role="admin")

    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc_info:
        reindex_document("doc-1", request, org)  # type: ignore[arg-type]

    assert exc_info.value.status_code == 409
    assert ingestion.submitted == []
