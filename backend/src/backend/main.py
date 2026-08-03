"""FastAPI 应用入口：装配依赖并提供文档、任务、文件和智能问答接口。"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated
from uuid import uuid4

import uvicorn
from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from . import __version__
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
    chat = ChatProvider(settings)
    ingestion = IngestionManager(settings, repository, parser, vector_index)
    service = RagService(settings, repository, ingestion, retriever, chat)
    return repository, parser, vector_index, ingestion, service


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    repository, parser, vector_index, ingestion, service = build_components(settings)
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
    return HealthOut(
        status="ok",
        app=app_settings.app_name,
        version=__version__,
        llm_configured=bool(app_settings.openai_api_key and app_settings.chat_model),
        embedding_configured=bool(app_settings.openai_api_key and app_settings.embedding_model),
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
