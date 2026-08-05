"""FastAPI 应用入口：装配依赖并提供文档、任务、文件和智能问答接口。"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterator
from contextlib import asynccontextmanager
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

logger = logging.getLogger("uvicorn.error")


def build_components(
    settings: Settings,
) -> tuple[
    Repository,
    DocumentParser,
    VectorIndex,
    IngestionManager,
    RagService,
]:
    database = Database(settings.sqlite_path)
    database.initialize()
    repository = Repository(database)
    parser = DocumentParser(settings)
    embeddings = EmbeddingProvider(settings)
    vector_index = VectorIndex(settings, embeddings)
    reranker = RerankProvider(settings)
    retriever = HybridRetriever(settings, repository, vector_index, reranker)
    agentic_retriever = AgenticRetriever(settings, repository, retriever)
    chat = ChatProvider(settings)
    ingestion = IngestionManager(settings, repository, parser, vector_index)
    service = RagService(settings, repository, ingestion, agentic_retriever, chat)
    return repository, parser, vector_index, ingestion, service


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
    settings = get_settings()
    repository, parser, vector_index, ingestion, service = build_components(settings)
    warmup_embeddings(settings, vector_index)
    app.state.settings = settings
    app.state.repository = repository
    app.state.parser = parser
    app.state.vector_index = vector_index
    app.state.ingestion = ingestion
    app.state.service = service
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
        "http://127.0.0.1:3000",
        "http://localhost:3000",
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
        scan_parser=app_settings.scan_parser,
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


@app.get("/api/documents/{document_id}/file")
def get_document_file(document_id: str, request: Request) -> FileResponse:
    document = repository(request).get_document(document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="文档不存在")
    path = Path(str(document["stored_path"]))
    if not path.exists():
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
    parsed_path = Path(str(document["parsed_path"])).resolve()
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
                    "retrieval_ms": round((retrieved_at - started_at) * 1000, 2),
                    **{
                        name: round(value, 2) if value is not None else None
                        for name, value in context.retrieval_timings.items()
                    },
                },
            )
            for delta in service.chat_provider.answer_stream(
                payload.question,
                context.hits,
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
                    context.hits,
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
