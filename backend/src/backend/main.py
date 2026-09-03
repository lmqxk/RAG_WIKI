"""FastAPI 应用入口：装配依赖并提供文档、任务、文件和智能问答接口。"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterator
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Annotated
from uuid import uuid4

import uvicorn
from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse

from . import __version__
from .agent import AgenticRetriever
from .config import Settings, get_settings
from .db import Database
from .ingestion import IngestionManager
from .parser import DocumentParser
from .providers import ChatProvider, EmbeddingProvider, RerankProvider
from .repository import Repository
from .retrieval import HybridRetriever
from .schemas import (
    ChatRequest,
    ChatResponse,
    DocumentMetadataUpdate,
    DocumentOut,
    HealthOut,
    JobOut,
    UploadOut,
)
from .service import RagService
from .vector_index import VectorIndex
from .wiki import WikiManager

logger = logging.getLogger("uvicorn.error")


def configure_file_logging() -> Path:
    log_path = get_settings().data_dir / "logs" / "backend.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    for logger_name in ("uvicorn", "uvicorn.access"):
        target = logging.getLogger(logger_name)
        if any(getattr(handler, "_rag_log_file", None) == log_path for handler in target.handlers):
            continue
        handler = RotatingFileHandler(
            log_path, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        handler._rag_log_file = log_path  # type: ignore[attr-defined]
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        target.addHandler(handler)
    return log_path


def build_components(
    settings: Settings,
) -> tuple[
    Repository,
    DocumentParser,
    VectorIndex,
    IngestionManager,
    RagService,
    WikiManager,
]:
    database = Database(settings.sqlite_path)
    database.initialize()
    repository = Repository(database)
    parser = DocumentParser(settings)
    embeddings = EmbeddingProvider(settings)
    vector_index = VectorIndex(settings, embeddings)
    reranker = RerankProvider(settings)
    retriever = HybridRetriever(settings, repository, vector_index, reranker)
    chat = ChatProvider(settings)
    wiki = WikiManager(settings, chat)
    agentic_retriever = AgenticRetriever(settings, repository, retriever, wiki)
    ingestion = IngestionManager(settings, repository, parser, vector_index, wiki)
    service = RagService(settings, repository, ingestion, agentic_retriever, chat)
    return repository, parser, vector_index, ingestion, service, wiki


def warmup_embeddings(settings: Settings, vector_index: VectorIndex) -> None:
    if not settings.embedding_warmup_on_start:
        return
    try:
        vector_index.embeddings.warmup()
        logger.info("embedding_warmup_done backend=%s", vector_index.embeddings.backend)
    except Exception:
        logger.warning("embedding_warmup_failed", exc_info=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    log_path = configure_file_logging()
    logger.info("file_logging_enabled path=%s", log_path)
    settings = get_settings()
    repository, parser, vector_index, ingestion, service, wiki = build_components(settings)
    warmup_embeddings(settings, vector_index)
    app.state.settings = settings
    app.state.repository = repository
    app.state.parser = parser
    app.state.vector_index = vector_index
    app.state.ingestion = ingestion
    app.state.service = service
    app.state.wiki = wiki
    ingestion.recover()
    yield
    ingestion.shutdown()
    vector_index.client.close()


app = FastAPI(
    title="规智库 API",
    description="规范与技术文档 Document Intelligence + Hybrid RAG",
    version=__version__,
    lifespan=lifespan,
)
settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        settings.frontend_origin,
        f"http://127.0.0.1:{settings.frontend_port}",
        f"http://localhost:{settings.frontend_port}",
    ],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


def repository(request: Request) -> Repository:
    return request.app.state.repository


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> FileResponse:
    path = Path(__file__).resolve().parents[3] / "frontend" / "public" / "favicon.svg"
    if not path.exists():
        raise HTTPException(status_code=404, detail="favicon not found")
    return FileResponse(path, media_type="image/svg+xml")


@app.get("/api/health", response_model=HealthOut)
def health(request: Request) -> HealthOut:
    app_settings: Settings = request.app.state.settings
    parser: DocumentParser = request.app.state.parser
    embeddings = request.app.state.vector_index.embeddings
    return HealthOut(
        status="ok",
        app=app_settings.app_name,
        version=__version__,
        llm_configured=bool(app_settings.openai_api_key and app_settings.chat_model),
        embedding_configured=embeddings.configured,
        embedding_backend=embeddings.backend,
        embedding_model=(
            app_settings.local_embedding_model_dir.name
            if embeddings.backend == "local" and embeddings.local_available
            else app_settings.embedding_model
        ),
        parser_available=parser.available(),
        mineru_available=parser.mineru.available(),
        rapidocr_available=parser.rapidocr.available(),
        document_pipeline=app_settings.document_pipeline,
    )


@app.get("/api/documents", response_model=list[DocumentOut])
def list_documents(request: Request) -> list[dict[str, object]]:
    return repository(request).list_documents()


@app.post("/api/documents", response_model=UploadOut, status_code=202)
async def upload_document(
    request: Request,
    file: Annotated[UploadFile, File()],
) -> UploadOut:
    service: RagService = request.app.state.service
    try:
        document, job_id, duplicate = await service.upload(file)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return UploadOut(
        document=DocumentOut.model_validate(document),
        job_id=job_id,
        duplicate=duplicate,
    )


@app.patch("/api/documents/{document_id}", response_model=DocumentOut)
def update_document_metadata(
    document_id: str,
    payload: DocumentMetadataUpdate,
    request: Request,
) -> dict[str, object]:
    repo = repository(request)
    if repo.get_document(document_id) is None:
        raise HTTPException(status_code=404, detail="文档不存在")
    values = payload.model_dump(exclude_none=True)
    repo.update_document(document_id, **values)
    document = repo.get_document(document_id)
    assert document is not None
    return document


@app.post("/api/documents/{document_id}/reparse", response_model=UploadOut, status_code=202)
async def reparse_document(
    document_id: str,
    request: Request,
    file: Annotated[UploadFile, File()],
) -> UploadOut:
    service: RagService = request.app.state.service
    try:
        document, job_id = await service.reparse_document(document_id, file)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return UploadOut(
        document=DocumentOut.model_validate(document),
        job_id=job_id,
        duplicate=False,
    )


@app.get("/api/jobs/{job_id}", response_model=JobOut)
def get_job(job_id: str, request: Request) -> dict[str, object]:
    job = repository(request).get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return job


def resolve_storage_path(raw: str, subdir: str, request: Request) -> Path | None:
    """按入库时保存的绝对路径定位文件；容器部署时绝对路径不可用，回退到挂载目录。

    数据库记录的是入库主机的绝对路径（如 E:\\...\\uploads\\x.pdf），
    Docker 容器内该路径不存在，需按文件名回退到挂载的 storage 子目录。
    历史数据可能在 uploads 下还有 default-org 等子目录，
    额外保留 subdir 之后的相对层级回退，避免按文件名找不到。
    Linux 容器中反斜杠不是路径分隔符，须先归一化才能取出文件名。
    """

    settings: Settings = request.app.state.settings
    normalized = raw.replace("\\", "/")
    candidates = [
        Path(normalized),
        settings.data_dir / subdir / Path(normalized).name,
    ]
    marker = f"/{subdir}/"
    offset = normalized.rfind(marker)
    if offset != -1:
        candidates.append(settings.data_dir / subdir / normalized[offset + len(marker):])
    return next((candidate for candidate in candidates if candidate.exists()), None)


@app.get("/api/documents/{document_id}/file")
def get_document_file(document_id: str, request: Request) -> FileResponse:
    document = repository(request).get_document(document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="文档不存在")
    path = resolve_storage_path(str(document["stored_path"]), "uploads", request)
    if path is None:
        raise HTTPException(status_code=404, detail="PDF 文件不存在")
    return FileResponse(
        path,
        media_type="application/pdf",
        filename=str(document["filename"]),
        content_disposition_type="inline",
    )


@app.get("/api/documents/{document_id}/asset")
def get_document_asset(
    document_id: str,
    request: Request,
    path: Annotated[str, Query(min_length=1)],
) -> FileResponse:
    document = repository(request).get_document(document_id)
    if document is None or not document.get("parsed_path"):
        raise HTTPException(status_code=404, detail="解析产物不存在")
    parsed_dir = resolve_storage_path(
        str(document["parsed_path"]), "parsed", request
    )
    if parsed_dir is None:
        raise HTTPException(status_code=404, detail="解析产物不存在")
    parsed_path = parsed_dir.resolve()
    asset_path = (parsed_path / path).resolve()
    try:
        asset_path.relative_to(parsed_path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="非法资源路径") from exc
    if not asset_path.exists() or not asset_path.is_file():
        raise HTTPException(status_code=404, detail="资源文件不存在")
    media_types = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
    }
    return FileResponse(asset_path, media_type=media_types.get(asset_path.suffix.lower()))


@app.post("/api/documents/{document_id}/reindex", response_model=JobOut, status_code=202)
def reindex_document(document_id: str, request: Request) -> dict[str, object]:
    repo = repository(request)
    if repo.get_document(document_id) is None:
        raise HTTPException(status_code=404, detail="文档不存在")
    job_id = str(uuid4())
    repo.create_job(job_id, document_id)
    request.app.state.ingestion.submit_reindex(job_id)
    job = repo.get_job(job_id)
    assert job is not None
    return job


@app.get("/api/wiki/concepts")
def wiki_concepts(request: Request) -> list[dict[str, object]]:
    wiki: WikiManager = request.app.state.wiki
    return wiki.concepts_overview()


@app.get("/api/wiki/concepts/{name}")
def wiki_concept_detail(name: str, request: Request) -> dict[str, object]:
    wiki: WikiManager = request.app.state.wiki
    repository_ = repository(request)
    target = next(
        (
            entry
            for entry in wiki.concept_lexicon()
            if str(entry["name"]) == name or name in entry["aliases"]
        ),
        None,
    )
    if target is None:
        raise HTTPException(status_code=404, detail="概念不存在")
    anchors = target["anchors"]
    assert isinstance(anchors, dict)
    chunk_ids = [chunk_id for ids in anchors.values() for chunk_id in ids]
    hits = repository_.get_chunks(chunk_ids)
    by_document: dict[str, list[dict[str, object]]] = {}
    for hit in hits:
        by_document.setdefault(hit.document_id, []).append(
            {
                "chunk_id": hit.chunk_id,
                "clause_no": hit.clause_no,
                "chapter_path": hit.chapter_path,
                "page_start": hit.page_start,
                "page_end": hit.page_end,
                "text": hit.text,
            }
        )
    document_info = {
        str(document["id"]): document for document in repository_.list_documents()
    }
    documents = [
        {
            "document_id": document_id,
            "title": document_info.get(document_id, {}).get("title", document_id),
            "standard_no": document_info.get(document_id, {}).get("standard_no"),
            "anchors": chunk_refs,
        }
        for document_id, chunk_refs in by_document.items()
    ]
    documents.sort(key=lambda item: -len(item["anchors"]))
    return {
        "name": target["name"],
        "aliases": target["aliases"],
        "documents": documents,
        "related": wiki.related_pages(f"concepts/{target['name']}"),
    }


@app.get("/api/wiki/graph")
def wiki_graph(
    request: Request,
    min_score: Annotated[float, Query(ge=0)] = 0.0,
    max_links: Annotated[int, Query(ge=1, le=1000)] = 300,
) -> dict[str, object]:
    wiki: WikiManager = request.app.state.wiki
    return wiki.graph_summary(min_score=min_score, max_links=max_links)


@app.get("/api/wiki/pages/{page_id:path}")
def wiki_page(page_id: str, request: Request) -> dict[str, object]:
    wiki: WikiManager = request.app.state.wiki
    page = wiki.read_page(page_id)
    if page is None:
        raise HTTPException(status_code=404, detail="Wiki 页面不存在")
    return page


@app.post("/api/chat", response_model=ChatResponse)
def chat(payload: ChatRequest, request: Request) -> ChatResponse:
    service: RagService = request.app.state.service
    try:
        return service.chat(payload.question, payload.document_ids)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"问答处理失败：{exc}") from exc


def _stream_event(event: str, data: dict[str, object]) -> str:
    return json.dumps({"event": event, **data}, ensure_ascii=False) + "\n"


def _write_chat_metrics(settings: Settings, metrics: dict[str, object]) -> None:
    log_dir = settings.data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    with (log_dir / "chat-metrics.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(metrics, ensure_ascii=False) + "\n")


@app.post("/api/chat/stream")
def chat_stream(payload: ChatRequest, request: Request) -> StreamingResponse:
    service: RagService = request.app.state.service
    settings: Settings = request.app.state.settings

    def generate() -> Iterator[str]:
        started_at = time.perf_counter()
        first_token_at: float | None = None
        answer_parts: list[str] = []
        try:
            context = service.chat_context(payload.question, payload.document_ids)
            retrieved_at = time.perf_counter()
            yield _stream_event(
                "meta",
                {
                    "trace_id": context.trace_id,
                    "query_type": context.query_type,
                    "evidence_status": context.evidence_status,
                    "used_external_llm": context.used_external_llm,
                    "citation_count": len(context.citations),
                    "retrieval_steps": context.retrieval_steps,
                    "retrieval_ms": round((retrieved_at - started_at) * 1000, 2),
                    **{
                        name: round(value, 2) if value is not None else None
                        for name, value in context.retrieval_timings.items()
                    },
                },
            )
            for delta in service.chat_provider.answer_stream(
                payload.question,
                context.cited_hits,
                context.query_type,
            ):
                if first_token_at is None:
                    first_token_at = time.perf_counter()
                    logger.info(
                        "chat_stream_first_token %s",
                        json.dumps(
                            {
                                "trace_id": context.trace_id,
                                "first_token_ms": round(
                                    (first_token_at - started_at) * 1000,
                                    2,
                                ),
                                "answer_llm_ttft_ms": round(
                                    (first_token_at - retrieved_at) * 1000,
                                    2,
                                ),
                                "hits": len(context.hits),
                                "llm": context.used_external_llm,
                            },
                            ensure_ascii=False,
                        ),
                    )
                answer_parts.append(delta)
                yield _stream_event("delta", {"text": delta})
            if context.used_external_llm and not answer_parts:
                fallback_answer = service.chat_provider.answer(
                    payload.question,
                    context.cited_hits,
                    context.query_type,
                )
                if fallback_answer:
                    first_token_at = first_token_at or time.perf_counter()
                    answer_parts.append(fallback_answer)
                    yield _stream_event("delta", {"text": fallback_answer})
            completed_at = time.perf_counter()
            answer = "".join(answer_parts).strip()
            response = service.chat_response_from_context(context, answer)
            total_ms = (completed_at - started_at) * 1000
            first_token_ms = (
                round((first_token_at - started_at) * 1000, 2)
                if first_token_at is not None
                else None
            )
            metrics = {
                "trace_id": context.trace_id,
                "retrieval_ms": round((retrieved_at - started_at) * 1000, 2),
                "first_token_ms": first_token_ms,
                "answer_llm_ttft_ms": (
                    round((first_token_at - retrieved_at) * 1000, 2)
                    if first_token_at is not None
                    else None
                ),
                **{
                    name: round(value, 2) if value is not None else None
                    for name, value in context.retrieval_timings.items()
                },
                "total_ms": round(total_ms, 2),
                "answer_chars": len(answer),
                "citations": len(context.citations),
                "query_type": context.query_type,
                "llm": context.used_external_llm,
            }
            _write_chat_metrics(settings, metrics)
            logger.info("chat_stream_done %s", json.dumps(metrics, ensure_ascii=False))
            yield _stream_event(
                "done",
                {
                    "result": response.model_dump(mode="json"),
                    "metrics": metrics,
                },
            )
        except Exception as exc:
            logger.exception("chat_stream_error")
            yield _stream_event("error", {"message": f"问答处理失败：{exc}"})

    return StreamingResponse(generate(), media_type="application/x-ndjson")


def run() -> None:
    app_settings = get_settings()
    uvicorn.run(
        "backend.main:app",
        host=app_settings.api_host,
        port=app_settings.api_port,
        reload=False,
    )


if __name__ == "__main__":
    run()
