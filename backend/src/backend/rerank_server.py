"""本地重排序服务：加载本地 Jina Reranker，并暴露 OpenAI/Jina 风格 rerank 接口。"""

from __future__ import annotations

import gc
import os
from threading import Lock
from typing import Any

from .torch_runtime import prepare_torch_runtime

prepare_torch_runtime()

import torch
import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from transformers import AutoModel, AutoTokenizer

from .config import get_settings


class RerankRequest(BaseModel):
    model: str
    query: str
    documents: list[str]
    top_n: int | None = Field(default=None, ge=1)
    return_documents: bool = False


class RerankResponse(BaseModel):
    model: str
    results: list[dict[str, Any]]


app = FastAPI(title="规智库本地重排序服务")
_model: Any | None = None
_device: str | None = None
_model_lock = Lock()
_inference_lock = Lock()


def preferred_devices(device: str) -> list[str]:
    normalized = device.lower()
    if normalized == "auto":
        return ["cuda", "cpu"] if torch.cuda.is_available() else ["cpu"]
    if normalized == "cuda":
        return ["cuda", "cpu"]
    return [normalized]


def load_model(force_device: str | None = None) -> Any:
    global _device, _model
    with _model_lock:
        if _model is not None and (force_device is None or _device == force_device):
            return _model
        settings = get_settings()
        if not settings.local_rerank_model_dir.exists():
            raise RuntimeError(f"重排序模型目录不存在: {settings.local_rerank_model_dir}")
        os.environ.setdefault("HF_HOME", str(settings.local_rerank_hf_home))
        os.environ.setdefault(
            "HF_MODULES_CACHE",
            str(settings.local_rerank_hf_home / "modules"),
        )
        errors: list[str] = []
        for device in preferred_devices(force_device or settings.local_rerank_device):
            try:
                model = AutoModel.from_pretrained(
                    settings.local_rerank_model_dir,
                    dtype="auto",
                    trust_remote_code=True,
                )
                attach_fixed_tokenizer(model, settings.local_rerank_model_dir)
                if device != "cpu":
                    model = model.to(device)
                model.eval()
                _model = model
                _device = device
                return _model
            except Exception as exc:
                errors.append(f"{device}: {exc}")
                _model = None
                _device = None
                gc.collect()
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
        raise RuntimeError("重排序模型加载失败：" + " | ".join(errors))


def attach_fixed_tokenizer(model: Any, model_dir: os.PathLike[str]) -> None:
    try:
        tokenizer = AutoTokenizer.from_pretrained(
            model_dir,
            trust_remote_code=True,
            fix_mistral_regex=True,
        )
    except TypeError:
        tokenizer = AutoTokenizer.from_pretrained(model_dir, trust_remote_code=True)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.unk_token
        tokenizer.pad_token_id = tokenizer.convert_tokens_to_ids(tokenizer.pad_token)
    tokenizer.padding_side = "left"
    model._tokenizer = tokenizer


@app.get("/health")
def health() -> dict[str, object]:
    settings = get_settings()
    return {
        "status": "ok",
        "model_dir": str(settings.local_rerank_model_dir),
        "configured_device": settings.local_rerank_device,
        "active_device": _device,
        "loaded": _model is not None,
    }


@app.post("/rerank", response_model=RerankResponse)
def rerank(payload: RerankRequest) -> RerankResponse:
    global _device, _model
    if not payload.documents:
        return RerankResponse(model=payload.model, results=[])
    try:
        with _inference_lock:
            model = load_model()
            try:
                with torch.inference_mode():
                    results = model.rerank(
                        payload.query,
                        payload.documents,
                        top_n=payload.top_n,
                    )
            except Exception as exc:
                if _device == "cuda":
                    with _model_lock:
                        _model = None
                        _device = None
                        gc.collect()
                        torch.cuda.empty_cache()
                    model = load_model(force_device="cpu")
                    with torch.inference_mode():
                        results = model.rerank(
                            payload.query,
                            payload.documents,
                            top_n=payload.top_n,
                        )
                else:
                    raise exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"本地重排序失败: {exc}") from exc

    normalized: list[dict[str, Any]] = []
    for result in results:
        item = {
            "index": int(result["index"]),
            "relevance_score": float(result["relevance_score"]),
        }
        if payload.return_documents:
            item["document"] = result.get("document")
        normalized.append(item)
    return RerankResponse(model=payload.model, results=normalized)


def run() -> None:
    settings = get_settings()
    uvicorn.run(
        "backend.rerank_server:app",
        host=settings.local_rerank_host,
        port=settings.local_rerank_port,
        reload=False,
    )


if __name__ == "__main__":
    run()
