"""存储后端抽象层：统一本地文件系统和 S3/MinIO 对象存储。

用法：
    storage = create_storage(settings)
    stored_path = storage.save_pdf("org-123", "doc-456", upload_stream)
    local_path = storage.get_pdf_path(stored_path)  # 自动下载到临时目录
    response = storage.serve_pdf(stored_path, "doc.pdf")  # FileResponse
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, BinaryIO

from .config import Settings


def _get_media_type(suffix: str) -> str:
    return {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".pdf": "application/pdf",
        ".json": "application/json",
        ".md": "text/markdown",
    }.get(suffix.lower(), "application/octet-stream")


# ── 抽象基类 ──────────────────────────────────────────────────────────────


class StorageBackend(ABC):
    """存储后端抽象基类。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    # ── PDF ────────────────────────────────────────────────────────────────

    @abstractmethod
    def save_pdf(
        self,
        organization_id: str,
        document_id: str,
        upload: Any,
        *,
        max_bytes: int,
    ) -> tuple[bytes, int, str]:
        """保存上传的 PDF 文件，返回 (sha256, file_size, stored_path)。

        upload 为 FastAPI UploadFile 对象，支持 async read()。
        """
        ...

    @abstractmethod
    def replace_pdf(
        self,
        stored_path: str,
        upload: Any,
        *,
        max_bytes: int,
    ) -> tuple[bytes, int, str, str]:
        """原子替换 PDF，返回 (sha256, file_size, new_path, backup_path)。"""
        ...

    @abstractmethod
    def delete_pdf(self, stored_path: str) -> None:
        """删除 PDF 文件。"""
        ...

    @abstractmethod
    def get_pdf_path(self, stored_path: str) -> Path:
        """返回 PDF 的本地可访问路径（解析器需要本地文件）。"""
        ...

    @abstractmethod
    def serve_pdf(self, stored_path: str, filename: str) -> Any:
        """返回 FastAPI FileResponse 或 RedirectResponse。"""
        ...

    # ── 解析产物 ───────────────────────────────────────────────────────────

    @abstractmethod
    def save_parsed(self, organization_id: str, document_id: str, parsed_dir: Path) -> str:
        """保存解析产物目录，返回 parsed_path。"""
        ...

    @abstractmethod
    def delete_parsed(self, parsed_path: str) -> None:
        """删除解析产物。"""
        ...

    @abstractmethod
    def get_parsed_path(self, parsed_path: str) -> Path:
        """返回解析产物的本地可访问路径。"""
        ...

    @abstractmethod
    def serve_asset(self, parsed_path: str, asset_path: str) -> Any:
        """返回 FastAPI FileResponse 或 RedirectResponse。"""
        ...

    # ── Wiki ────────────────────────────────────────────────────────────────

    @abstractmethod
    def save_wiki(self, wiki_dir: Path) -> None:
        """上传 Wiki 目录到存储。"""
        ...

    @abstractmethod
    def get_wiki_path(self) -> Path:
        """返回 Wiki 的本地可访问路径。"""
        ...


# ── 本地文件系统实现 ──────────────────────────────────────────────────────


class LocalStorage(StorageBackend):
    """本地文件系统存储后端（默认）。"""

    def _uploads_dir(self, organization_id: str) -> Path:
        path = self.settings.data_dir / "uploads" / organization_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _parsed_dir(self, organization_id: str, document_id: str) -> Path:
        path = self.settings.data_dir / "parsed" / organization_id / document_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _abs_path(self, stored_path: str) -> Path:
        p = Path(stored_path)
        return p if p.is_absolute() else (self.settings.data_dir / p).resolve()

    def save_pdf(
        self,
        organization_id: str,
        document_id: str,
        upload: Any,
        *,
        max_bytes: int,
    ) -> tuple[bytes, int, str]:
        import hashlib

        target = self._uploads_dir(organization_id) / f"{document_id}.pdf"
        digest = hashlib.sha256()
        file_size = 0
        with target.open("wb") as f:
            first = True
            while True:
                chunk = upload.file.read(1024 * 1024)
                if not chunk:
                    break
                if first:
                    if b"%PDF-" not in chunk[:1024]:
                        raise ValueError("文件不是有效的 PDF")
                    first = False
                file_size += len(chunk)
                if file_size > max_bytes:
                    target.unlink(missing_ok=True)
                    raise ValueError(f"文件超过 {self.settings.max_file_size_mb} MB 上限")
                digest.update(chunk)
                f.write(chunk)
        if file_size == 0:
            target.unlink(missing_ok=True)
            raise ValueError("文件不能为空")
        return digest.digest(), file_size, str(target)

    def replace_pdf(
        self,
        stored_path: str,
        upload: Any,
        *,
        max_bytes: int,
    ) -> tuple[bytes, int, str, str]:
        import hashlib
        from uuid import uuid4

        target = self._abs_path(stored_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = target.with_name(f".{target.name}.{uuid4().hex}.upload")
        backup_path = target.with_name(f".{target.name}.{uuid4().hex}.backup")
        digest = hashlib.sha256()
        file_size = 0
        with tmp_path.open("xb") as f:
            while True:
                chunk = upload.file.read(1024 * 1024)
                if not chunk:
                    break
                file_size += len(chunk)
                if file_size > max_bytes:
                    tmp_path.unlink(missing_ok=True)
                    raise ValueError(f"文件超过 {self.settings.max_file_size_mb} MB 上限")
                digest.update(chunk)
                f.write(chunk)
        if file_size == 0:
            tmp_path.unlink(missing_ok=True)
            raise ValueError("文件不能为空")
        os.replace(target, backup_path)
        try:
            os.replace(tmp_path, target)
        except Exception:
            os.replace(backup_path, target)
            raise
        return digest.digest(), file_size, str(target), str(backup_path)

    def delete_pdf(self, stored_path: str) -> None:
        self._abs_path(stored_path).unlink(missing_ok=True)

    def get_pdf_path(self, stored_path: str) -> Path:
        return self._abs_path(stored_path)

    def serve_pdf(self, stored_path: str, filename: str) -> Any:
        from fastapi.responses import FileResponse

        path = self._abs_path(stored_path)
        if not path.exists():
            from fastapi import HTTPException
            raise HTTPException(status_code=404, detail="PDF 文件不存在")
        return FileResponse(
            path,
            media_type="application/pdf",
            filename=filename,
            content_disposition_type="inline",
        )

    def save_parsed(self, organization_id: str, document_id: str, parsed_dir: Path) -> str:
        target = self._parsed_dir(organization_id, document_id)
        # 如果目标已存在，先清空再复制
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(parsed_dir, target, dirs_exist_ok=True)
        return str(target)

    def delete_parsed(self, parsed_path: str) -> None:
        path = self._abs_path(parsed_path)
        if path.exists():
            shutil.rmtree(path)

    def get_parsed_path(self, parsed_path: str) -> Path:
        return self._abs_path(parsed_path)

    def serve_asset(self, parsed_path: str, asset_relative: str) -> Any:
        from fastapi import HTTPException
        from fastapi.responses import FileResponse

        parsed = self._abs_path(parsed_path).resolve()
        asset = (parsed / asset_relative).resolve()
        try:
            asset.relative_to(parsed)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="非法资源路径") from exc
        if not asset.exists() or not asset.is_file():
            raise HTTPException(status_code=404, detail="资源文件不存在")
        return FileResponse(asset, media_type=_get_media_type(asset.suffix))

    def save_wiki(self, wiki_dir: Path) -> None:
        # 本地模式下 wiki 已在原地写入
        pass

    def get_wiki_path(self) -> Path:
        return self.settings.data_dir / "wiki"


# ── S3/MinIO 实现 ────────────────────────────────────────────────────────


class S3Storage(StorageBackend):
    """S3/MinIO 对象存储后端。

    使用临时目录缓存文件以满足解析器需要本地路径的要求。
    """

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        import boto3

        self._s3 = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint,
            aws_access_key_id=settings.s3_access_key,
            aws_secret_access_key=settings.s3_secret_key,
            region_name=settings.s3_region or "us-east-1",
        )
        self.bucket = settings.s3_bucket
        self._cache_dir = settings.data_dir / ".s3-cache"
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._ensure_bucket()

    def _ensure_bucket(self) -> None:
        try:
            self._s3.head_bucket(Bucket=self.bucket)
        except Exception:
            self._s3.create_bucket(Bucket=self.bucket)

    def _key(self, *parts: str) -> str:
        return "/".join(parts)

    def _cache_path(self, key: str) -> Path:
        return self._cache_dir / key

    def _download(self, key: str) -> Path:
        local = self._cache_path(key)
        if local.exists():
            return local
        local.parent.mkdir(parents=True, exist_ok=True)
        self._s3.download_file(self.bucket, key, str(local))
        return local

    def _upload(self, key: str, local_path: Path) -> None:
        self._s3.upload_file(str(local_path), self.bucket, key)

    def _upload_dir(self, prefix: str, local_dir: Path) -> None:
        for path in local_dir.rglob("*"):
            if path.is_file():
                relative = path.relative_to(local_dir).as_posix()
                self._upload(self._key(prefix, relative), path)

    def _delete_prefix(self, prefix: str) -> None:
        objects = self._s3.list_objects_v2(Bucket=self.bucket, Prefix=prefix)
        if "Contents" in objects:
            self._s3.delete_objects(
                Bucket=self.bucket,
                Delete={"Objects": [{"Key": obj["Key"]} for obj in objects["Contents"]]},
            )

    def save_pdf(
        self,
        organization_id: str,
        document_id: str,
        upload: Any,
        *,
        max_bytes: int,
    ) -> tuple[bytes, int, str]:
        import hashlib
        import tempfile

        key = self._key("uploads", organization_id, f"{document_id}.pdf")
        digest = hashlib.sha256()
        file_size = 0
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
            first = True
            while True:
                chunk = upload.file.read(1024 * 1024)
                if not chunk:
                    break
                if first:
                    if b"%PDF-" not in chunk[:1024]:
                        tmp.close()
                        Path(tmp.name).unlink(missing_ok=True)
                        raise ValueError("文件不是有效的 PDF")
                    first = False
                file_size += len(chunk)
                if file_size > max_bytes:
                    tmp.close()
                    Path(tmp.name).unlink(missing_ok=True)
                    raise ValueError(f"文件超过 {self.settings.max_file_size_mb} MB 上限")
                digest.update(chunk)
                tmp.write(chunk)
        if file_size == 0:
            Path(tmp.name).unlink(missing_ok=True)
            raise ValueError("文件不能为空")
        # 上传到 S3
        self._s3.upload_file(tmp.name, self.bucket, key)
        Path(tmp.name).unlink(missing_ok=True)
        # 缓存到本地
        cache_local = self._cache_path(key)
        cache_local.parent.mkdir(parents=True, exist_ok=True)
        self._download(key)
        return digest.digest(), file_size, f"s3://{self.bucket}/{key}"

    def replace_pdf(
        self,
        stored_path: str,
        upload: Any,
        *,
        max_bytes: int,
    ) -> tuple[bytes, int, str, str]:
        import hashlib
        import tempfile
        from uuid import uuid4

        bucket, key = self._parse_s3_path(stored_path)
        backup_key = f"{key}.{uuid4().hex}.backup"
        # 备份旧文件
        self._s3.copy_object(Bucket=bucket, CopySource={"Bucket": bucket, "Key": key}, Key=backup_key)
        # 读新文件
        digest = hashlib.sha256()
        file_size = 0
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
            while True:
                chunk = upload.file.read(1024 * 1024)
                if not chunk:
                    break
                file_size += len(chunk)
                if file_size > max_bytes:
                    tmp.close()
                    Path(tmp.name).unlink(missing_ok=True)
                    raise ValueError(f"文件超过 {self.settings.max_file_size_mb} MB 上限")
                digest.update(chunk)
                tmp.write(chunk)
        if file_size == 0:
            Path(tmp.name).unlink(missing_ok=True)
            raise ValueError("文件不能为空")
        # 上传新文件
        self._s3.upload_file(tmp.name, bucket, key)
        Path(tmp.name).unlink(missing_ok=True)
        # 更新缓存
        cache_local = self._cache_path(key)
        if cache_local.exists():
            cache_local.unlink()
        self._download(key)
        return digest.digest(), file_size, stored_path, f"s3://{bucket}/{backup_key}"

    @staticmethod
    def _parse_s3_path(s3_path: str) -> tuple[str, str]:
        parts = s3_path.removeprefix("s3://").split("/", 1)
        return parts[0], parts[1] if len(parts) > 1 else ""

    def delete_pdf(self, stored_path: str) -> None:
        bucket, key = self._parse_s3_path(stored_path)
        self._s3.delete_object(Bucket=bucket, Key=key)
        cache_local = self._cache_path(key)
        if cache_local.exists():
            cache_local.unlink()

    def get_pdf_path(self, stored_path: str) -> Path:
        bucket, key = self._parse_s3_path(stored_path)
        return self._download(key)

    def serve_pdf(self, stored_path: str, filename: str) -> Any:
        bucket, key = self._parse_s3_path(stored_path)
        from fastapi.responses import RedirectResponse

        url = self._s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": key, "ResponseContentDisposition": f"inline; filename=\"{filename}\""},
            ExpiresIn=3600,
        )
        return RedirectResponse(url)

    def save_parsed(self, organization_id: str, document_id: str, parsed_dir: Path) -> str:
        prefix = self._key("parsed", organization_id, document_id)
        self._upload_dir(prefix, parsed_dir)
        return f"s3://{self.bucket}/{prefix}"

    def delete_parsed(self, parsed_path: str) -> None:
        bucket, prefix = self._parse_s3_path(parsed_path)
        self._delete_prefix(prefix)
        # 清理缓存
        cache_prefix = self._cache_path(prefix)
        if cache_prefix.exists():
            shutil.rmtree(cache_prefix)

    def get_parsed_path(self, parsed_path: str) -> Path:
        bucket, prefix = self._parse_s3_path(parsed_path)
        local = self._cache_path(prefix)
        if local.exists():
            return local
        local.mkdir(parents=True, exist_ok=True)
        # 下载所有文件
        paginator = self._s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                relative = Path(key).relative_to(prefix)
                target = local / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                self._s3.download_file(bucket, key, str(target))
        return local

    def serve_asset(self, parsed_path: str, asset_relative: str) -> Any:
        bucket, prefix = self._parse_s3_path(parsed_path)
        key = f"{prefix}/{asset_relative}"
        from fastapi.responses import RedirectResponse

        url = self._s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": key},
            ExpiresIn=3600,
        )
        return RedirectResponse(url)

    def save_wiki(self, wiki_dir: Path) -> None:
        prefix = "wiki"
        self._upload_dir(prefix, wiki_dir)

    def get_wiki_path(self) -> Path:
        local = self._cache_path("wiki")
        if not local.exists():
            # 首次使用，从 S3 下载
            self._download_prefix("wiki", local)
        return local

    def _download_prefix(self, prefix: str, local_dir: Path) -> None:
        import boto3

        local_dir.mkdir(parents=True, exist_ok=True)
        s3 = self._s3
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                relative = Path(key).relative_to(prefix) if prefix else Path(key)
                target = local_dir / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                s3.download_file(self.bucket, key, str(target))


# ── 工厂函数 ──────────────────────────────────────────────────────────────


def create_storage(settings: Settings) -> StorageBackend:
    """根据配置创建存储后端。

    可通过 ``RAG_STORAGE_BACKEND=s3`` 切换为 S3/MinIO 对象存储。
    """
    backend = settings.storage_backend or "local"
    if backend == "s3":
        return S3Storage(settings)
    return LocalStorage(settings)