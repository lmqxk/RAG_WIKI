"""FastAPI 应用入口：装配依赖并提供文档、任务、文件和智能问答接口。"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from logging.handlers import RotatingFileHandler
from typing import Annotated
from uuid import uuid4

import uvicorn
from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from . import __version__
from .agent import AgenticRetriever
from .auth import create_access_token, decode_access_token, verify_password
from .config import Settings, get_settings
from .db import Database
from .events import EVENT_JOB_UPDATED, EventBus, get_event_bus, make_event, close_event_bus
from .ingestion import IngestionManager
from .parser import DocumentParser
from .providers import ChatProvider, EmbeddingProvider, RerankProvider
from .repository import DEFAULT_ORGANIZATION_ID, Repository
from .retrieval import HybridRetriever
from .schemas import (
    AccessTokenOut,
    AddMemberRequest,
    AuditLogOut,
    ChatRequest,
    ChatResponse,
    CreateOrganizationRequest,
    DocumentMetadataUpdate,
    DocumentOut,
    HealthOut,
    JobOut,
    LoginRequest,
    MemberOut,
    OrganizationOut,
    RegisterOut,
    RegisterRequest,
    UpdateMemberRoleRequest,
    UploadOut,
)
from .service import RagService
from .storage import create_storage, StorageBackend
from .vector_index import VectorIndex
from .wiki import WikiManager

logger = logging.getLogger("uvicorn.error")
PROJECT_ROOT = Path(__file__).resolve().parents[3]


def configure_file_logging() -> Path:
    """将后端与 Uvicorn 日志写入项目目录，并按大小轮转。"""
    log_path = PROJECT_ROOT / "logs" / "backend.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    for target_name in ("uvicorn", "uvicorn.access"):
        target = logging.getLogger(target_name)
        if any(getattr(handler, "_rag_log_file", None) == log_path for handler in target.handlers):
            continue
        handler = RotatingFileHandler(
            log_path,
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        handler._rag_log_file = log_path  # type: ignore[attr-defined]
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        target.addHandler(handler)
    return log_path


def build_components(
    settings: Settings,
    storage: StorageBackend | None = None,
    event_bus: EventBus | None = None,
) -> tuple[
    Repository,
    DocumentParser,
    VectorIndex,
    IngestionManager,
    RagService,
]:
    database = Database(settings.sqlite_path, database_url=settings.database_url)
    database.initialize()
    repository = Repository(database, event_bus=event_bus)
    if storage is None:
        storage = create_storage(settings)
    parser = DocumentParser(settings)
    embeddings = EmbeddingProvider(settings)
    vector_index = VectorIndex(settings, embeddings)
    reranker = RerankProvider(settings)
    retriever = HybridRetriever(settings, repository, vector_index, reranker)
    chat = ChatProvider(settings)
    wiki = WikiManager(settings, chat, storage=storage)
    agentic_retriever = AgenticRetriever(settings, repository, retriever, wiki)
    ingestion = IngestionManager(settings, repository, parser, vector_index, wiki, storage=storage)
    service = RagService(settings, repository, ingestion, agentic_retriever, chat, storage=storage)
    return repository, parser, vector_index, ingestion, service


def warmup_embeddings(settings: Settings, vector_index: VectorIndex) -> None:
    if not settings.embedding_warmup_on_start:
        return
    try:
        vector_index.embeddings.warmup()
        logger.info("embedding_warmup_done backend=%s", vector_index.embeddings.backend)
    except Exception:
        logger.warning("embedding_warmup_failed", exc_info=True)


def bootstrap_authentication(settings: Settings, repository: Repository) -> None:
    """认证启用时校验密钥，并按显式环境变量创建首个管理员。"""

    if not settings.auth_enabled:
        return
    if not settings.auth_jwt_secret:
        raise RuntimeError("认证已启用，但未配置 RAG_AUTH_JWT_SECRET")
    username = settings.auth_bootstrap_username
    password = settings.auth_bootstrap_password
    if bool(username) != bool(password):
        raise RuntimeError("管理员引导创建必须同时配置用户名和密码")
    if username and password:
        repository.ensure_bootstrap_admin(username, password)


@asynccontextmanager
async def lifespan(app: FastAPI):
    log_path = configure_file_logging()
    logger.info("file_logging_enabled path=%s", log_path)
    settings = get_settings()
    storage = create_storage(settings)
    # 初始化事件总线
    event_bus = await get_event_bus(settings)
    repository, parser, vector_index, ingestion, service = build_components(settings, storage, event_bus)
    bootstrap_authentication(settings, repository)
    warmup_embeddings(settings, vector_index)
    app.state.settings = settings
    app.state.storage = storage
    app.state.event_bus = event_bus
    app.state.repository = repository
    app.state.parser = parser
    app.state.vector_index = vector_index
    app.state.ingestion = ingestion
    app.state.service = service
    ingestion.recover()
    yield
    ingestion.shutdown()
    vector_index.client.close()
    await close_event_bus()


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

_PUBLIC_API_PATHS = {"/api/health", "/api/auth/login", "/favicon.ico", "/openapi.json", "/docs"}


@app.middleware("http")
async def authenticate_request(request: Request, call_next):
    # OPTIONS 预检请求直接放行，让 CORS 中间件处理
    if request.method == "OPTIONS":
        return await call_next(request)
    settings: Settings = getattr(request.app.state, "settings", get_settings())
    if not settings.auth_enabled or request.url.path in _PUBLIC_API_PATHS:
        return await call_next(request)
    authorization = request.headers.get("Authorization", "")
    scheme, _, token = authorization.partition(" ")
    claims = (
        decode_access_token(token, settings.auth_jwt_secret or "")
        if scheme.lower() == "bearer" and token
        else None
    )
    user_id = str(claims.get("sub", "")) if claims else ""
    repository_instance: Repository | None = getattr(request.app.state, "repository", None)
    user = repository_instance.get_user_by_id(user_id) if repository_instance and user_id else None
    if user is None or not bool(user["is_active"]):
        return JSONResponse(status_code=401, content={"detail": "未登录或登录已过期"})
    organization_id = request.headers.get("X-Organization-ID", DEFAULT_ORGANIZATION_ID).strip()
    membership = repository_instance.get_membership(organization_id, user_id)
    if membership is None:
        return JSONResponse(status_code=403, content={"detail": "无权访问该组织"})
    request.state.user = user
    request.state.organization_id = organization_id
    request.state.role = membership["role"]
    return await call_next(request)


@app.post("/api/auth/login", response_model=AccessTokenOut)
def login(payload: LoginRequest, request: Request) -> AccessTokenOut:
    settings: Settings = request.app.state.settings
    if not settings.auth_enabled or not settings.auth_jwt_secret:
        raise HTTPException(status_code=404, detail="认证未启用")
    user = repository(request).get_user_by_username(payload.username)
    if user is None or not bool(user["is_active"]) or not verify_password(
        payload.password, str(user["password_hash"])
    ):
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    token = create_access_token(
        {"sub": str(user["id"]), "username": str(user["username"])},
        settings.auth_jwt_secret,
        settings.auth_access_token_minutes * 60,
    )
    return AccessTokenOut(access_token=token)


@app.post("/api/auth/register", response_model=RegisterOut, status_code=201)
def register(payload: RegisterRequest, request: Request) -> RegisterOut:
    """自助注册新用户。

    注册成功后用户自动加入指定组织（或默认组织）的 viewer 角色。
    仅在认证启用时可用。
    """
    settings: Settings = request.app.state.settings
    if not settings.auth_enabled:
        raise HTTPException(status_code=404, detail="认证未启用")
    repo = repository(request)

    existing = repo.get_user_by_username(payload.username)
    if existing is not None:
        raise HTTPException(status_code=409, detail="用户名已存在")

    org_id = payload.organization_id or DEFAULT_ORGANIZATION_ID
    org = repo.get_organization(org_id)
    if org is None:
        raise HTTPException(status_code=404, detail="组织不存在")

    user = repo.create_user(payload.username, payload.password)
    repo.add_member(org_id, str(user["id"]), "viewer")
    return RegisterOut(id=str(user["id"]), username=str(user["username"]))


def repository(request: Request) -> Repository:
    return request.app.state.repository


def organization_id(request: Request) -> str:
    return getattr(request.state, "organization_id", DEFAULT_ORGANIZATION_ID)


@dataclass(slots=True)
class OrganizationContext:
    """从认证中间件提取的组织上下文，供 API 端点注入。"""

    organization_id: str
    user_id: str
    role: str


def get_organization_context(request: Request) -> OrganizationContext:
    """FastAPI 依赖：从 request.state 提取已认证的组织上下文。"""
    return OrganizationContext(
        organization_id=getattr(request.state, "organization_id", DEFAULT_ORGANIZATION_ID),
        user_id=getattr(request.state, "user", {}).get("id", ""),
        role=getattr(request.state, "role", "viewer"),
    )


_ROLE_RANK = {"viewer": 0, "editor": 1, "admin": 2}


def require_role(minimum_role: str):
    """返回 FastAPI 依赖，校验当前用户角色是否 >= minimum_role。

    认证未启用时跳过校验，保持向后兼容。
    """

    def _check(
        request: Request,
        org: Annotated[OrganizationContext, Depends(get_organization_context)],
    ) -> OrganizationContext:
        settings: Settings = getattr(request.app.state, "settings", get_settings())
        if not settings.auth_enabled:
            return org
        if _ROLE_RANK.get(org.role, -1) < _ROLE_RANK.get(minimum_role, 0):
            raise HTTPException(status_code=403, detail="权限不足")
        return org

    return _check


def _write_audit_log(
    request: Request,
    org: OrganizationContext,
    action: str,
    resource_type: str,
    resource_id: str | None = None,
    detail: str | None = None,
) -> None:
    """写入审计日志；仅认证启用时记录。"""
    settings: Settings = getattr(request.app.state, "settings", get_settings())
    if not settings.auth_enabled:
        return
    repo = repository(request)
    ip = request.client.host if request.client else None
    repo.create_audit_log(
        organization_id=org.organization_id,
        user_id=org.user_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        detail=detail,
        ip_address=ip,
    )


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
        auth_enabled=bool(app_settings.auth_enabled and app_settings.auth_jwt_secret),
        llm_configured=bool(app_settings.openai_api_key and app_settings.chat_model),
        embedding_configured=bool(embeddings),
        embedding_backend=app_settings.embedding_backend,
        embedding_model=app_settings.embedding_model,
        parser_available=parser.available(),
        mineru_available=parser.mineru.available(),
        rapidocr_available=parser.rapidocr.available(),
        document_pipeline=app_settings.document_pipeline,
    )


@app.get("/api/events")
async def job_events(request: Request):
    """SSE 端点：实时推送任务进度更新。

    前端通过 EventSource 连接：
        const es = new EventSource('/api/events');
        es.addEventListener('job_updated', (e) => {
            const data = JSON.parse(e.data);
            // data.job_id, data.status, data.stage, data.progress, data.message
        });
    """
    event_bus: EventBus = request.app.state.event_bus
    queue = await event_bus.subscribe("jobs")

    async def generate():
        try:
            while True:
                message = await queue.get()
                yield message
        except asyncio.CancelledError:
            pass
        finally:
            await event_bus.unsubscribe("jobs", queue)

    return StreamingResponse(generate(), media_type="text/event-stream")


@app.get("/api/documents", response_model=list[DocumentOut])
def list_documents(request: Request) -> list[dict[str, object]]:
    return repository(request).list_documents(organization_id(request))


@app.post("/api/documents", response_model=UploadOut, status_code=202)
async def upload_document(
    request: Request,
    file: Annotated[UploadFile, File()],
    org: Annotated[OrganizationContext, Depends(require_role("editor"))],
) -> UploadOut:
    service: RagService = request.app.state.service
    try:
        document, job_id, duplicate = await service.upload(
            file, organization_id=org.organization_id
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _write_audit_log(
        request, org, "document.upload", "document",
        resource_id=str(document.get("id", "")),
        detail=f"filename={file.filename}",
    )
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
    org: Annotated[OrganizationContext, Depends(require_role("editor"))],
) -> dict[str, object]:
    repo = repository(request)
    if repo.get_document(document_id, organization_id=org.organization_id) is None:
        raise HTTPException(status_code=404, detail="文档不存在")
    values = payload.model_dump(exclude_none=True)
    repo.update_document(document_id, **values)
    document = repo.get_document(document_id, organization_id=org.organization_id)
    assert document is not None
    _write_audit_log(
        request, org, "document.update", "document",
        resource_id=document_id,
        detail=f"fields={list(values.keys())}",
    )
    return document


@app.post("/api/documents/{document_id}/reparse", response_model=UploadOut, status_code=202)
async def reparse_document(
    document_id: str,
    request: Request,
    file: Annotated[UploadFile, File()],
    org: Annotated[OrganizationContext, Depends(require_role("editor"))],
) -> UploadOut:
    service: RagService = request.app.state.service
    try:
        document, job_id = await service.reparse_document(
            document_id, file, organization_id=org.organization_id
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _write_audit_log(
        request, org, "document.reparse", "document",
        resource_id=document_id,
    )
    return UploadOut(
        document=DocumentOut.model_validate(document),
        job_id=job_id,
        duplicate=False,
    )


@app.get("/api/jobs/{job_id}", response_model=JobOut)
def get_job(
    job_id: str,
    request: Request,
    org: Annotated[OrganizationContext, Depends(get_organization_context)],
) -> dict[str, object]:
    job = repository(request).get_job(job_id, organization_id=org.organization_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return job


@app.get("/api/documents/{document_id}/file")
def get_document_file(
    document_id: str,
    request: Request,
    org: Annotated[OrganizationContext, Depends(get_organization_context)],
):
    document = repository(request).get_document(
        document_id, organization_id=org.organization_id
    )
    if document is None:
        raise HTTPException(status_code=404, detail="文档不存在")
    storage: StorageBackend = request.app.state.storage
    return storage.serve_pdf(
        str(document["stored_path"]),
        str(document["filename"]),
    )


@app.get("/api/documents/{document_id}/asset")
def get_document_asset(
    document_id: str,
    request: Request,
    path: Annotated[str, Query(min_length=1)],
    org: Annotated[OrganizationContext, Depends(get_organization_context)],
):
    document = repository(request).get_document(
        document_id, organization_id=org.organization_id
    )
    if document is None or not document.get("parsed_path"):
        raise HTTPException(status_code=404, detail="解析产物不存在")
    storage: StorageBackend = request.app.state.storage
    return storage.serve_asset(str(document["parsed_path"]), path)


@app.post("/api/documents/{document_id}/reindex", response_model=JobOut, status_code=202)
def reindex_document(
    document_id: str,
    request: Request,
    org: Annotated[OrganizationContext, Depends(require_role("editor"))],
) -> dict[str, object]:
    repo = repository(request)
    if repo.get_document(document_id, organization_id=org.organization_id) is None:
        raise HTTPException(status_code=404, detail="文档不存在")
    job_id = str(uuid4())
    job, created = repo.create_job_if_idle(job_id, document_id)
    if not created:
        raise HTTPException(status_code=409, detail="文档已有正在执行的任务")
    request.app.state.ingestion.submit_reindex(job_id)
    _write_audit_log(
        request, org, "document.reindex", "document",
        resource_id=document_id,
    )
    return job


@app.post("/api/chat", response_model=ChatResponse)
def chat(
    payload: ChatRequest,
    request: Request,
    org: Annotated[OrganizationContext, Depends(get_organization_context)],
) -> ChatResponse:
    service: RagService = request.app.state.service
    try:
        return service.chat(
            payload.question,
            payload.document_ids,
            organization_id=org.organization_id,
        )
    except Exception:
        logger.exception("chat_error")
        raise HTTPException(status_code=500, detail="问答处理失败，请稍后重试") from None


def _stream_event(event: str, data: dict[str, object]) -> str:
    return json.dumps({"event": event, **data}, ensure_ascii=False) + "\n"


def _write_chat_metrics(settings: Settings, metrics: dict[str, object]) -> None:
    log_dir = settings.data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    with (log_dir / "chat-metrics.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(metrics, ensure_ascii=False) + "\n")


@app.post("/api/chat/stream")
def chat_stream(
    payload: ChatRequest,
    request: Request,
    org: Annotated[OrganizationContext, Depends(get_organization_context)],
) -> StreamingResponse:
    service: RagService = request.app.state.service
    settings: Settings = request.app.state.settings

    def generate() -> Iterator[str]:
        started_at = time.perf_counter()
        first_token_at: float | None = None
        answer_parts: list[str] = []
        try:
            context = service.chat_context(
                payload.question,
                payload.document_ids,
                organization_id=org.organization_id,
            )
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
        except Exception:
            logger.exception("chat_stream_error")
            yield _stream_event("error", {"message": "问答处理失败，请稍后重试"})

    return StreamingResponse(generate(), media_type="application/x-ndjson")


# ── 组织管理（仅 admin） ─────────────────────────────────────────────────


@app.post("/api/admin/organizations", response_model=OrganizationOut, status_code=201)
def create_organization(
    payload: CreateOrganizationRequest,
    request: Request,
    org: Annotated[OrganizationContext, Depends(require_role("admin"))],
) -> dict[str, object]:
    repo = repository(request)
    created = repo.create_organization(payload.name)
    _write_audit_log(
        request, org, "organization.create", "organization",
        resource_id=str(created["id"]),
        detail=f"name={payload.name}",
    )
    return created


@app.get("/api/admin/organizations", response_model=list[OrganizationOut])
def list_organizations(
    request: Request,
    org: Annotated[OrganizationContext, Depends(require_role("admin"))],
) -> list[dict[str, object]]:
    return repository(request).list_organizations()


@app.get("/api/admin/organizations/{org_id}/members", response_model=list[MemberOut])
def list_members(
    org_id: str,
    request: Request,
    org: Annotated[OrganizationContext, Depends(require_role("admin"))],
) -> list[dict[str, object]]:
    return repository(request).list_members(org_id)


@app.post(
    "/api/admin/organizations/{org_id}/members",
    response_model=MemberOut,
    status_code=201,
)
def add_member(
    org_id: str,
    payload: AddMemberRequest,
    request: Request,
    org: Annotated[OrganizationContext, Depends(require_role("admin"))],
) -> dict[str, object]:
    repo = repository(request)
    user = repo.get_user_by_username(payload.username)
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    membership = repo.add_member(org_id, str(user["id"]), payload.role)
    if membership is None:
        raise HTTPException(status_code=409, detail="该用户已是组织成员")
    _write_audit_log(
        request, org, "organization.add_member", "organization_member",
        resource_id=org_id,
        detail=f"username={payload.username}, role={payload.role}",
    )
    row = repo.list_members(org_id)
    for member in row:
        if member["user_id"] == user["id"]:
            return member
    raise HTTPException(status_code=500, detail="添加成员后查询失败")


@app.delete(
    "/api/admin/organizations/{org_id}/members/{user_id}",
    status_code=204,
)
def remove_member(
    org_id: str,
    user_id: str,
    request: Request,
    org: Annotated[OrganizationContext, Depends(require_role("admin"))],
) -> None:
    if org_id == DEFAULT_ORGANIZATION_ID:
        raise HTTPException(status_code=400, detail="不能从默认组织中移除成员")
    repo = repository(request)
    if not repo.remove_member(org_id, user_id):
        raise HTTPException(status_code=404, detail="成员不存在")
    _write_audit_log(
        request, org, "organization.remove_member", "organization_member",
        resource_id=org_id,
        detail=f"user_id={user_id}",
    )


@app.patch(
    "/api/admin/organizations/{org_id}/members/{user_id}",
    response_model=MemberOut,
)
def update_member_role(
    org_id: str,
    user_id: str,
    payload: UpdateMemberRoleRequest,
    request: Request,
    org: Annotated[OrganizationContext, Depends(require_role("admin"))],
) -> dict[str, object]:
    repo = repository(request)
    membership = repo.update_member_role(org_id, user_id, payload.role)
    if membership is None:
        raise HTTPException(status_code=404, detail="成员不存在")
    _write_audit_log(
        request, org, "organization.update_role", "organization_member",
        resource_id=org_id,
        detail=f"user_id={user_id}, role={payload.role}",
    )
    rows = repo.list_members(org_id)
    for member in rows:
        if member["user_id"] == user_id:
            return member
    raise HTTPException(status_code=500, detail="更新角色后查询失败")


@app.get("/api/admin/audit-logs", response_model=list[AuditLogOut])
def list_audit_logs(
    request: Request,
    org: Annotated[OrganizationContext, Depends(require_role("admin"))],
    limit: int = Query(default=50, ge=1, le=200),
) -> list[dict[str, object]]:
    return repository(request).list_audit_logs(org.organization_id, limit=limit)


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
