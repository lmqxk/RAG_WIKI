"""验证 API 层文件响应行为。"""

from pathlib import Path
from types import SimpleNamespace

from backend.config import Settings
from backend.db import Database
from backend.main import favicon, get_document_file
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
    # settings.data_dir 指向 tmp_path 模拟 storage 挂载目录，覆盖绝对路径回退逻辑。
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path,
        sqlite_path=tmp_path / "rag.db",
    )
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(repository=repository, settings=settings)
        )
    )

    response = get_document_file("doc-1", request)  # type: ignore[arg-type]

    assert response.media_type == "application/pdf"
    assert response.headers["content-disposition"].startswith("inline;")


def test_document_file_falls_back_to_mounted_storage_dir(tmp_path: Path) -> None:
    """容器部署：入库绝对路径不存在时，按文件名回退到挂载目录。"""

    database = Database(tmp_path / "rag.db")
    database.initialize()
    repository = Repository(database)
    mounted = tmp_path / "storage" / "uploads"
    mounted.mkdir(parents=True)
    (mounted / "doc-2.pdf").write_bytes(b"%PDF-1.4")
    repository.create_document(
        document_id="doc-2",
        filename="test2.pdf",
        stored_path=tmp_path / "legacy-host" / "uploads" / "doc-2.pdf",
        sha256="def",
        title="test2",
        standard_no=None,
        version=None,
        file_size=8,
    )
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path / "storage",
        sqlite_path=tmp_path / "rag.db",
    )
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(repository=repository, settings=settings)
        )
    )

    response = get_document_file("doc-2", request)  # type: ignore[arg-type]

    assert response.media_type == "application/pdf"


def test_document_file_falls_back_preserving_subdir(tmp_path: Path) -> None:
    """历史数据带 default-org 子目录：保留相对层级回退，而非仅按文件名。"""

    database = Database(tmp_path / "rag.db")
    database.initialize()
    repository = Repository(database)
    legacy_dir = tmp_path / "storage" / "uploads" / "default-org"
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "doc-3.pdf").write_bytes(b"%PDF-1.4")
    repository.create_document(
        document_id="doc-3",
        filename="test3.pdf",
        stored_path=r"E:\old-host\storage\uploads\default-org\doc-3.pdf",
        sha256="ghi",
        title="test3",
        standard_no=None,
        version=None,
        file_size=8,
    )
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path / "storage",
        sqlite_path=tmp_path / "rag.db",
    )
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(repository=repository, settings=settings)
        )
    )

    response = get_document_file("doc-3", request)  # type: ignore[arg-type]

    assert response.media_type == "application/pdf"


def test_favicon_response_uses_frontend_svg() -> None:
    response = favicon()

    assert response.media_type == "image/svg+xml"
