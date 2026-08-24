"""应用配置模块：集中管理服务、解析、模型、检索和存储参数。"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    """仅从环境变量读取配置，不自动创建或修改 .env。"""

    model_config = SettingsConfigDict(
        env_prefix="RAG_",
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "规智库"
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    frontend_origin: str = "http://localhost:3000"
    frontend_host: str = "127.0.0.1"
    frontend_port: int = 3000
    auth_enabled: bool = False
    auth_jwt_secret: str | None = None
    auth_access_token_minutes: int = 60
    auth_bootstrap_username: str | None = None
    auth_bootstrap_password: str | None = None

    data_dir: Path = PROJECT_ROOT / "storage"
    max_file_size_mb: int = 100
    max_pdf_pages: int = 600
    max_workers: int = 1
    document_parse_timeout_seconds: int = 1800

    document_pipeline: Literal["paddlevl", "pdf-extract-kit", "mineru", "rapidocr", "pymupdf"] = (
        "paddlevl"
    )
    ocr_render_dpi: int = 180
    mineru_command: str | None = None
    mineru_api_url: str | None = "http://127.0.0.1:8001"
    mineru_auto_start: bool = True
    mineru_compose_dir: Path = PROJECT_ROOT / "deploy" / "mineru"
    mineru_method: str = "auto"
    mineru_ocr_lang: str = "ch"
    mineru_model_source: str = "modelscope"
    mineru_model_dir: Path = PROJECT_ROOT / ".models"
    paddlevl_api_url: str | None = "http://127.0.0.1:8080"
    paddlevl_auto_start: bool = True
    paddlevl_compose_dir: Path = PROJECT_ROOT / "deploy" / "paddlevl"

    openai_base_url: str = "https://api.openai.com/v1"
    openai_api_key: str | None = None
    chat_model: str | None = None
    chat_max_tokens: int = 2048
    chat_think: bool | None = None
    embedding_backend: str = "local"
    embedding_model: str | None = None
    embedding_base_url: str = "https://api.jina.ai/v1"
    embedding_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("RAG_EMBEDDING_API_KEY", "JINA_API_KEY"),
    )
    embedding_dimension: int = 1024
    embedding_batch_size: int = 16
    embedding_collection_name: str = "document_chunks_jina_embeddings_v5_small_v1"
    embedding_warmup_on_start: bool = True
    embedding_fallback_enabled: bool = True
    embedding_fallback_base_url: str = "https://api.siliconflow.cn/v1"
    embedding_fallback_api_key: str | None = None
    embedding_fallback_model: str = "BAAI/bge-large-zh-v1.5"
    embedding_fallback_dimension: int = 1024
    embedding_hf_fallback_enabled: bool = True
    embedding_hf_api_token: str | None = None
    embedding_hf_model: str = "BAAI/bge-small-zh-v1.5"
    embedding_hf_provider: str = "hf-inference"
    huggingface_fallback_enabled: bool = True
    huggingface_embedding_model: str = "BAAI/bge-small-zh-v1.5"
    huggingface_api_token: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "RAG_HUGGINGFACEHUB_API_TOKEN",
            "HUGGINGFACEHUB_API_TOKEN",
        ),
    )
    embedding_hf_fallback_enabled: bool = True
    embedding_hf_api_token: str | None = None
    embedding_hf_model: str = "BAAI/bge-small-zh-v1.5"
    embedding_hf_provider: str = "hf-inference"
    local_embedding_model_dir: Path = (
        PROJECT_ROOT
        / ".models"
        / "modelscope"
        / "models"
        / "BAAI--bge-small-zh-v1.5"
    )
    local_embedding_device: str = "auto"

    rerank_base_url: str | None = "http://127.0.0.1:8011/rerank"
    rerank_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("RAG_RERANK_API_KEY", "JINA_API_KEY"),
    )
    rerank_model: str | None = "jina-reranker-v3"
    rerank_fallback_enabled: bool = True
    rerank_fallback_url: str = "https://api.siliconflow.cn/v1/rerank"
    rerank_fallback_api_key: str | None = None
    rerank_fallback_model: str = "BAAI/bge-reranker-v2-m3"
    local_rerank_model_dir: Path = (
        PROJECT_ROOT
        / ".models"
        / "modelscope"
        / "models"
        / "jinaai--jina-reranker-v3.5"
        / "snapshots"
        / "master"
    )
    local_rerank_host: str = "127.0.0.1"
    local_rerank_port: int = 8011
    local_rerank_device: str = "auto"
    local_rerank_warmup_on_start: bool = True
    local_rerank_hf_home: Path = PROJECT_ROOT / ".models" / "huggingface"

    retrieval_bm25_top_k: int = 20
    retrieval_dense_top_k: int = 20
    retrieval_fused_top_k: int = 20
    retrieval_final_top_k: int = 8
    answer_max_citations: int = 6
    agentic_retrieval_enabled: bool = True
    agentic_planner_llm_enabled: bool = True
    agentic_max_steps: int = 4
    agentic_parallel_workers: int = 4
    agentic_min_hits: int = 4

    # Wiki 是基于已解析原文生成的派生知识层，不参与 PDF 二次解析。
    wiki_enabled: bool = True
    wiki_llm_enabled: bool = True
    wiki_analysis_max_source_chars: int = 7000

    sqlite_path: Path | None = Field(default=None)
    qdrant_path: Path | None = Field(default=None)
    qdrant_url: str | None = Field(default=None)
    qdrant_api_key: str | None = Field(default=None)
    database_url: str | None = Field(default=None)

    # 对象存储
    storage_backend: str | None = Field(default=None)  # "local" 或 "s3"
    s3_endpoint: str | None = Field(default=None)
    s3_bucket: str = "rag-zb"
    s3_access_key: str | None = Field(default=None)
    s3_secret_key: str | None = Field(default=None)
    s3_region: str | None = Field(default=None)

    # 任务队列
    task_backend: str | None = Field(default=None)  # "threadpool" 或 "celery"
    redis_url: str | None = Field(default=None)

    def model_post_init(self, __context: object) -> None:
        self.data_dir = self.data_dir.resolve()
        self.sqlite_path = (self.sqlite_path or self.data_dir / "rag.db").resolve()
        self.qdrant_path = (self.qdrant_path or self.data_dir / "qdrant").resolve()
        self.mineru_model_dir = self.mineru_model_dir.resolve()
        self.mineru_compose_dir = self.mineru_compose_dir.resolve()
        self.paddlevl_compose_dir = self.paddlevl_compose_dir.resolve()
        self.local_rerank_model_dir = self.local_rerank_model_dir.resolve()
        self.local_rerank_hf_home = self.local_rerank_hf_home.resolve()
        self.local_embedding_model_dir = self.local_embedding_model_dir.resolve()
        if self.database_url and self.database_url.startswith("sqlite"):
            sqlite_path = self.database_url.removeprefix("sqlite:///")
            self.sqlite_path = Path(sqlite_path).resolve()

    @property
    def qdrant_is_remote(self) -> bool:
        """是否使用远程 Qdrant Server。"""
        return bool(self.qdrant_url)

    def ensure_directories(self) -> None:
        paths = [
            self.data_dir,
            self.data_dir / "uploads",
            self.data_dir / "parsed",
            self.data_dir / "wiki",
            self.mineru_model_dir,
            self.local_rerank_hf_home,
        ]
        if not self.qdrant_is_remote:
            paths.append(self.data_dir / "qdrant")
        if self.storage_backend == "s3":
            paths.append(self.data_dir / ".s3-cache")
        for path in paths:
            path.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_directories()
    return settings
