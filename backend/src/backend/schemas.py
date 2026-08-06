"""API 数据模型模块：定义请求、响应、任务状态、文档元数据和引用结构。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class DocumentOut(BaseModel):
    id: str
    filename: str
    title: str
    standard_no: str | None = None
    version: str | None = None
    status: str
    page_count: int
    file_size: int
    needs_ocr: bool
    parser_name: str | None = None
    error: str | None = None
    created_at: datetime
    updated_at: datetime


class UploadOut(BaseModel):
    document: DocumentOut
    job_id: str
    duplicate: bool = False


class JobOut(BaseModel):
    id: str
    document_id: str
    status: str
    stage: str
    progress: float = Field(ge=0, le=100)
    message: str
    error: str | None = None
    created_at: datetime
    updated_at: datetime


class DocumentMetadataUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=300)
    standard_no: str | None = Field(default=None, max_length=100)
    version: str | None = Field(default=None, max_length=100)


class ChatRequest(BaseModel):
    question: str = Field(min_length=2, max_length=2000)
    document_ids: list[str] | None = None


class Citation(BaseModel):
    index: int
    chunk_id: str
    document_id: str
    document_title: str
    standard_no: str | None = None
    version: str | None = None
    clause_no: str | None = None
    chapter_path: str
    pdf_page: int
    printed_page: str | None = None
    quote: str
    score: float
    source_type: str
    preview_image_url: str | None = None
    preview_label: str | None = None


class ChatResponse(BaseModel):
    answer: str
    citations: list[Citation]
    evidence_status: str
    query_type: str
    used_external_llm: bool
    trace_id: str


class HealthOut(BaseModel):
    status: str
    app: str
    version: str
    llm_configured: bool
    embedding_configured: bool
    embedding_backend: str
    embedding_model: str | None
    parser_available: bool
    mineru_available: bool
    rapidocr_available: bool
    document_pipeline: str
