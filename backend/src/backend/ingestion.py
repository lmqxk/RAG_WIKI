"""文档入库模块：编排 PDF 解析、结构化切分、索引写入和任务状态更新。"""

from __future__ import annotations

import json
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .chunking import build_chunks
from .config import Settings
from .domain import PageBlock, ParsedDocument
from .parser import DocumentParser
from .repository import Repository
from .vector_index import VectorIndex


class IngestionManager:
    def __init__(
        self,
        settings: Settings,
        repository: Repository,
        parser: DocumentParser,
        vector_index: VectorIndex,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.parser = parser
        self.vector_index = vector_index
        self.executor = ThreadPoolExecutor(
            max_workers=settings.max_workers,
            thread_name_prefix="rag-ingestion",
        )
        self.running: set[str] = set()

    def recover(self) -> None:
        for job in self.repository.pending_jobs():
            self.submit(str(job["id"]))

    def submit(self, job_id: str) -> None:
        if job_id in self.running:
            return
        self.running.add(job_id)
        self.executor.submit(self._process, job_id)

    def submit_reindex(self, job_id: str) -> None:
        if job_id in self.running:
            return
        self.running.add(job_id)
        self.executor.submit(self._reindex, job_id)

    def shutdown(self) -> None:
        self.executor.shutdown(wait=False, cancel_futures=False)

    def _process(self, job_id: str) -> None:
        try:
            job = self.repository.get_job(job_id)
            if job is None:
                return
            document_id = str(job["document_id"])
            document = self.repository.get_document(document_id)
            if document is None:
                raise RuntimeError("文档记录不存在")
            self.repository.update_job(
                job_id,
                status="RUNNING",
                stage="preflight",
                progress=2,
                message="检查 PDF 文件",
            )
            self.repository.update_document(document_id, status="PARSING", error=None)

            parsed_dir = self.settings.data_dir / "parsed" / document_id

            def progress(value: float, message: str) -> None:
                self.repository.update_job(
                    job_id,
                    status="RUNNING",
                    stage="parsing",
                    progress=value,
                    message=message,
                )

            parsed = self.parser.parse(Path(str(document["stored_path"])), parsed_dir, progress)
            self._save_parsed(parsed_dir, parsed)
            self.repository.update_document(
                document_id,
                page_count=parsed.pages,
                needs_ocr=int(parsed.needs_ocr),
                parser_name=parsed.parser_name,
                parsed_path=str(parsed_dir),
                status="INDEXING",
            )

            self.repository.update_job(
                job_id,
                status="RUNNING",
                stage="chunking",
                progress=62,
                message="按章节和条款切片",
            )
            chunks = build_chunks(document_id, parsed.blocks)
            if not chunks:
                raise RuntimeError("解析完成但未生成有效条款，请检查 PDF 解析结果")
            self.repository.replace_chunks(document_id, chunks)

            self.repository.update_job(
                job_id,
                status="RUNNING",
                stage="indexing",
                progress=72,
                message=f"建立混合检索索引，共 {len(chunks)} 个片段",
            )
            self.vector_index.replace_document(document_id, chunks)
            self.repository.update_document(document_id, status="READY", error=None)
            self.repository.update_job(
                job_id,
                status="COMPLETED",
                stage="completed",
                progress=100,
                message=f"解析完成，共 {parsed.pages} 页、{len(chunks)} 个检索片段",
            )
        except Exception as exc:
            job = self.repository.get_job(job_id)
            if job:
                document_id = str(job["document_id"])
                self.repository.update_document(
                    document_id,
                    status="FAILED",
                    error=str(exc),
                )
            self.repository.update_job(
                job_id,
                status="FAILED",
                stage="failed",
                message="文档处理失败",
                error=f"{exc}\n{traceback.format_exc(limit=8)}",
            )
        finally:
            self.running.discard(job_id)

    def _reindex(self, job_id: str) -> None:
        try:
            job = self.repository.get_job(job_id)
            if job is None:
                return
            document_id = str(job["document_id"])
            document = self.repository.get_document(document_id)
            if document is None or not document.get("parsed_path"):
                raise RuntimeError("没有可复用的结构化解析结果，请重新上传文档")
            parsed_path = Path(str(document["parsed_path"]))
            normalized_path = parsed_path / "normalized.json"
            if not normalized_path.exists():
                raise RuntimeError("结构化解析结果不存在，请重新上传文档")

            self.repository.update_document(document_id, status="INDEXING", error=None)
            self.repository.update_job(
                job_id,
                status="RUNNING",
                stage="chunking",
                progress=40,
                message="复用解析结果，重新识别章节与条款",
            )
            payload = json.loads(normalized_path.read_text(encoding="utf-8"))
            blocks = [
                PageBlock(
                    page=int(block["page"]),
                    text=str(block["text"]),
                    block_type=str(block.get("type", "text")),
                    bbox=[float(value) for value in block.get("bbox", [])],
                    level=block.get("level"),
                    printed_page=block.get("printed_page"),
                    images=list(block.get("images", [])),
                )
                for block in payload["blocks"]
            ]
            chunks = build_chunks(document_id, blocks)
            if not chunks:
                raise RuntimeError("重新切片后没有生成有效条款")
            self.repository.replace_chunks(document_id, chunks)
            self.repository.update_job(
                job_id,
                status="RUNNING",
                stage="indexing",
                progress=72,
                message=f"重建混合检索索引，共 {len(chunks)} 个片段",
            )
            self.vector_index.replace_document(document_id, chunks)
            self.repository.update_document(document_id, status="READY", error=None)
            self.repository.update_job(
                job_id,
                status="COMPLETED",
                stage="completed",
                progress=100,
                message=f"重新索引完成，共 {len(chunks)} 个检索片段",
            )
        except Exception as exc:
            job = self.repository.get_job(job_id)
            if job:
                self.repository.update_document(
                    str(job["document_id"]),
                    status="FAILED",
                    error=str(exc),
                )
            self.repository.update_job(
                job_id,
                status="FAILED",
                stage="failed",
                message="重新索引失败",
                error=f"{exc}\n{traceback.format_exc(limit=8)}",
            )
        finally:
            self.running.discard(job_id)

    def _save_parsed(self, output_dir: Path, parsed: ParsedDocument) -> None:
        output_dir.mkdir(parents=True, exist_ok=True)
        normalized = {
            "pages": parsed.pages,
            "parser_name": parsed.parser_name,
            "needs_ocr": parsed.needs_ocr,
            "blocks": [
                {
                    "page": block.page,
                    "text": block.text,
                    "type": block.block_type,
                    "bbox": block.bbox,
                    "level": block.level,
                    "printed_page": block.printed_page,
                    "images": block.images,
                }
                for block in parsed.blocks
            ],
        }
        (output_dir / "normalized.json").write_text(
            json.dumps(normalized, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (output_dir / "document.md").write_text(parsed.markdown, encoding="utf-8")
