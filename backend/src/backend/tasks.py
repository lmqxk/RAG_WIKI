"""Celery 任务队列：替代 ThreadPoolExecutor 执行异步文档处理任务。

用法：
    # 启动 Celery Worker
    celery -A backend.tasks worker --loglevel=info

    # 在代码中调用
    from .tasks import process_document, reindex_document
    process_document.delay(job_id)
"""

from __future__ import annotations

import logging
from typing import Any

from celery import Celery

from .config import get_settings

logger = logging.getLogger(__name__)

# ── Celery 应用 ────────────────────────────────────────────────────────────


def create_celery_app() -> Celery:
    """从配置创建 Celery 应用。"""
    settings = get_settings()
    redis_url = settings.redis_url or "redis://localhost:6379/0"
    app = Celery(
        "rag_zb",
        broker=redis_url,
        backend=redis_url,
    )
    app.conf.update(
        task_serializer="json",
        accept_content=["json"],
        result_serializer="json",
        timezone="UTC",
        enable_utc=True,
        task_track_started=True,
        task_soft_time_limit=settings.document_parse_timeout_seconds,
        task_time_limit=settings.document_parse_timeout_seconds + 60,
        worker_prefetch_multiplier=1,
        worker_max_tasks_per_child=settings.max_workers or 1,
    )
    return app


# 全局单例（延迟初始化）
_celery_app: Celery | None = None


def get_celery_app() -> Celery:
    global _celery_app
    if _celery_app is None:
        _celery_app = create_celery_app()
    return _celery_app


# ── 依赖构建（懒加载，供 Celery Worker 在子进程中调用） ─────────────────────


def _build_components_for_task() -> dict[str, Any]:
    """在 Celery Worker 中构建任务所需的组件。"""
    from .config import get_settings
    from .db import Database
    from .parser import DocumentParser
    from .providers import ChatProvider, EmbeddingProvider, RerankProvider
    from .repository import Repository
    from .retrieval import HybridRetriever
    from .storage import create_storage
    from .vector_index import VectorIndex
    from .wiki import WikiManager

    settings = get_settings()
    database = Database(settings.sqlite_path, database_url=settings.database_url)
    database.initialize()
    repository = Repository(database)
    storage = create_storage(settings)
    embeddings = EmbeddingProvider(settings)
    vector_index = VectorIndex(settings, embeddings)
    parser = DocumentParser(settings)
    wiki = WikiManager(settings, ChatProvider(settings), storage=storage)
    return {
        "settings": settings,
        "repository": repository,
        "storage": storage,
        "parser": parser,
        "vector_index": vector_index,
        "wiki": wiki,
    }


# ── 任务定义 ────────────────────────────────────────────────────────────────


def _run_process(job_id: str) -> None:
    """同步执行文档解析任务（此函数可被 Celery 或线程池调用）。"""
    from .chunking import build_chunks

    comps = _build_components_for_task()
    repo = comps["repository"]
    storage = comps["storage"]
    parser = comps["parser"]
    vector_index = comps["vector_index"]
    wiki = comps["wiki"]

    job = repo.get_job(job_id)
    if job is None:
        return
    document_id = str(job["document_id"])
    document = repo.get_document(document_id)
    if document is None:
        raise RuntimeError("文档记录不存在")
    organization_id = str(document.get("organization_id", "default-org"))

    repo.update_job(job_id, status="RUNNING", stage="preflight", progress=2, message="检查 PDF 文件")
    repo.update_document(document_id, status="PARSING", error=None)

    parsed_dir = comps["settings"].data_dir / "parsed" / organization_id / document_id
    last_parse_progress = 2.0

    def progress(value: float, message: str) -> None:
        nonlocal last_parse_progress
        last_parse_progress = max(last_parse_progress, value)
        repo.update_job(job_id, status="RUNNING", stage="parsing", progress=last_parse_progress, message=message)

    parsed = parser.parse(storage.get_pdf_path(str(document["stored_path"])), parsed_dir, progress)
    # 保存解析产物
    from .ingestion import save_parsed_output

    save_parsed_output(parsed_dir, parsed)
    parsed_path = storage.save_parsed(organization_id, document_id, parsed_dir)

    repo.update_document(
        document_id, page_count=parsed.pages, needs_ocr=int(parsed.needs_ocr),
        parser_name=parsed.parser_name, parsed_path=parsed_path, status="INDEXING",
    )
    repo.update_job(job_id, status="RUNNING", stage="chunking", progress=62, message="按章节和条款切片")

    chunks = build_chunks(document_id, parsed.blocks)
    if not chunks:
        raise RuntimeError("解析完成但未生成有效条款，请检查 PDF 解析结果")

    repo.update_job(
        job_id, status="RUNNING", stage="indexing", progress=72,
        message=f"建立混合检索索引，共 {len(chunks)} 个片段",
    )
    vector_snapshot = vector_index.replace_document(document_id, chunks, organization_id=organization_id)
    try:
        repo.replace_chunks(document_id, chunks)
    except Exception:
        vector_index.restore_document(document_id, vector_snapshot)
        raise

    if wiki:
        repo.update_job(job_id, status="RUNNING", stage="wiki", progress=88, message="基于已解析原文生成 Wiki 知识页")
        from .ingestion import build_parsed_from_payload, load_parsed_payload

        payload = load_parsed_payload(parsed_dir)
        parsed_doc = build_parsed_from_payload(payload, blocks=parsed.blocks)
        wiki.sync_document(document, parsed_doc, chunks, event="ingest")

    repo.update_document(document_id, status="READY", error=None)
    repo.update_job(
        job_id, status="COMPLETED", stage="completed", progress=100,
        message=f"解析完成，共 {parsed.pages} 页、{len(chunks)} 个检索片段",
    )


def _run_reindex(job_id: str) -> None:
    """同步执行文档重建索引任务。"""
    from .chunking import build_chunks

    comps = _build_components_for_task()
    repo = comps["repository"]
    storage = comps["storage"]
    vector_index = comps["vector_index"]
    wiki = comps["wiki"]

    job = repo.get_job(job_id)
    if job is None:
        return
    document_id = str(job["document_id"])
    document = repo.get_document(document_id)
    if document is None or not document.get("parsed_path"):
        raise RuntimeError("没有可复用的结构化解析结果，请重新上传文档")
    organization_id = str(document.get("organization_id", "default-org"))

    parsed_path = storage.get_parsed_path(str(document["parsed_path"]))
    from .ingestion import load_parsed_payload

    payload = load_parsed_payload(parsed_path)
    if payload is None:
        raise RuntimeError("结构化解析结果不存在，请重新上传文档")

    repo.update_document(document_id, status="INDEXING", error=None)
    repo.update_job(job_id, status="RUNNING", stage="chunking", progress=40, message="复用解析结果，重新识别章节与条款")

    from .domain import PageBlock

    blocks = [
        PageBlock(
            page=int(block["page"]),
            text=str(block["text"]),
            block_type=str(block.get("type", "text")),
            bbox=[float(v) for v in block.get("bbox", [])],
            level=block.get("level"),
            printed_page=block.get("printed_page"),
            images=list(block.get("images", [])),
        )
        for block in payload["blocks"]
    ]
    chunks = build_chunks(document_id, blocks)
    if not chunks:
        raise RuntimeError("重新切片后没有生成有效条款")

    repo.update_job(job_id, status="RUNNING", stage="indexing", progress=72, message=f"重建混合检索索引，共 {len(chunks)} 个片段")
    vector_snapshot = vector_index.replace_document(document_id, chunks, organization_id=organization_id)
    try:
        repo.replace_chunks(document_id, chunks)
    except Exception:
        vector_index.restore_document(document_id, vector_snapshot)
        raise

    if wiki:
        from .ingestion import build_parsed_from_payload

        parsed_doc = build_parsed_from_payload(payload, blocks=blocks)
        repo.update_job(job_id, status="RUNNING", stage="wiki", progress=88, message="基于已有解析结果生成 Wiki 知识页")
        wiki.sync_document(document, parsed_doc, chunks, event="reindex")

    repo.update_document(document_id, status="READY", error=None)
    repo.update_job(job_id, status="COMPLETED", stage="completed", progress=100, message="重建索引完成")


# ── Celery 任务注册 ────────────────────────────────────────────────────────


def register_tasks(app: Celery) -> None:
    """将任务注册到 Celery 应用。"""

    @app.task(bind=True, max_retries=0, name="process_document")
    def process_document_task(self, job_id: str) -> None:
        logger.info("process_document_task start job_id=%s", job_id)
        try:
            _run_process(job_id)
        except Exception:
            logger.exception("process_document_task failed job_id=%s", job_id)
            raise

    @app.task(bind=True, max_retries=0, name="reindex_document")
    def reindex_document_task(self, job_id: str) -> None:
        logger.info("reindex_document_task start job_id=%s", job_id)
        try:
            _run_reindex(job_id)
        except Exception:
            logger.exception("reindex_document_task failed job_id=%s", job_id)
            raise


# 初始化默认实例
_celery = get_celery_app()
register_tasks(_celery)

# 导出任务函数
process_document = _celery.tasks["process_document"]
reindex_document = _celery.tasks["reindex_document"]