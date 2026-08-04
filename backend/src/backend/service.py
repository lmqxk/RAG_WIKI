"""业务服务模块：连接知识库检索、回答生成、引用整理和证据充分性判断。"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from sqlite3 import IntegrityError
from urllib.parse import quote
from uuid import uuid4

from fastapi import UploadFile

from .agent import AgenticRetriever
from .config import Settings
from .domain import SearchHit
from .ingestion import IngestionManager
from .providers import ChatProvider
from .repository import Repository
from .schemas import ChatResponse, Citation

STANDARD_RE = re.compile(r"(?i)\b((?:GB|JGJ|CJJ|DL|NB|JT|DB|Q)[/\s-]*T?[\s-]*\d{3,8}(?:-\d{4})?)\b")
VERSION_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})(?:\s*年|\s*版)?")
HTML_IMAGE_RE = re.compile(r"""<img[^>]+src=["']([^"']+)["']""", re.IGNORECASE)


@dataclass(slots=True)
class ChatContext:
    trace_id: str
    question: str
    query_type: str
    hits: list[SearchHit]
    citations: list[Citation]
    evidence_status: str
    used_external_llm: bool
    retrieval_timings: dict[str, float | None]


def infer_metadata(filename: str) -> tuple[str, str | None, str | None]:
    stem = Path(filename).stem.strip()
    standard_match = STANDARD_RE.search(stem)
    standard_no = re.sub(r"\s+", " ", standard_match.group(1)).upper() if standard_match else None
    versions = VERSION_RE.findall(stem)
    version = versions[-1] if versions else None
    bracketed = re.search(r"《([^》]+)》", stem)
    if bracketed:
        title = bracketed.group(1).strip()
    else:
        title = STANDARD_RE.sub("", stem)
        title = re.sub(r"[（(]?\s*(?:19|20)\d{2}\s*版?\s*[）)]?", "", title)
        title = title.strip(" 《》-_")
    return title, standard_no, version


def preview_from_parsed(
    document: dict[str, object] | None,
    page: int,
    source_type: str,
    evidence_text: str | None = None,
    question: str | None = None,
) -> tuple[str | None, str | None]:
    if document is None or "image" not in source_type and "table" not in source_type:
        return None, None
    parsed_path_value = document.get("parsed_path")
    if not parsed_path_value:
        return None, None
    parsed_path = Path(str(parsed_path_value))
    if not parsed_path.exists():
        return None, None
    preview = _preview_from_normalized(
        document,
        parsed_path,
        page,
        source_type,
        question,
        evidence_text,
    )
    if preview[0]:
        return preview
    for content_path in sorted(parsed_path.rglob("*content_list.json")):
        try:
            content = json.loads(content_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if evidence_text:
            preview = _preview_from_evidence_paths(
                document,
                parsed_path,
                content_path.parent,
                evidence_text,
                source_type,
                question,
            )
            if preview[0]:
                return preview
        for item in content:
            if int(item.get("page_idx", -1)) + 1 != page:
                continue
            item_type = str(item.get("type", "")).lower()
            if "table" in source_type and item_type != "table":
                continue
            if "image" in source_type and item_type not in {"image", "img", "figure"}:
                continue
            for raw_path in _item_image_paths(item):
                resolved = (content_path.parent / raw_path).resolve()
                try:
                    relative = resolved.relative_to(parsed_path.resolve()).as_posix()
                except ValueError:
                    continue
                if resolved.exists() and resolved.is_file():
                    label = "表格截图" if "table" in source_type else "图片内容"
                    document_id = quote(str(document["id"]), safe="")
                    asset_path = quote(relative, safe="/")
                    return f"/api/documents/{document_id}/asset?path={asset_path}", label
    return None, None


def _preview_from_normalized(
    document: dict[str, object],
    parsed_path: Path,
    page: int,
    source_type: str,
    question: str | None,
    evidence_text: str | None,
) -> tuple[str | None, str | None]:
    normalized_path = parsed_path / "normalized.json"
    if not normalized_path.exists():
        return None, None
    try:
        payload = json.loads(normalized_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, None
    candidates: list[tuple[int, dict[str, object]]] = []
    for block in payload.get("blocks", []):
        if not isinstance(block, dict) or int(block.get("page", -1)) != page:
            continue
        block_type = str(block.get("type", "")).lower()
        if "table" in source_type and block_type != "table":
            continue
        if "image" in source_type and block_type not in {"image", "img", "figure"}:
            continue
        for image in block.get("images", []):
            if isinstance(image, dict) and image.get("path"):
                candidates.append(
                    (
                        _image_metadata_score(image, question, evidence_text),
                        image,
                    )
                )
    for _score, image in sorted(candidates, key=lambda item: item[0], reverse=True):
        for base_path in _image_base_paths(parsed_path):
            preview = _asset_preview(
                document,
                parsed_path,
                base_path / str(image["path"]),
                source_type,
            )
            if preview[0]:
                return preview
    return None, None


def _image_base_paths(parsed_path: Path) -> list[Path]:
    bases = [path.parent for path in sorted(parsed_path.rglob("*content_list.json"))]
    bases.append(parsed_path)
    return list(dict.fromkeys(bases))


def _image_metadata_score(
    image: dict[str, object],
    question: str | None,
    evidence_text: str | None,
) -> int:
    caption = str(image.get("caption", ""))
    context = " ".join(
        str(image.get(key, ""))
        for key in ("row_context", "column_context", "cell_text")
    )
    score = _text_match_score(question or "", caption) * 10
    score += _text_match_score(question or "", context) * 2
    if evidence_text:
        score += _text_match_score(evidence_text, caption)
        score += _text_match_score(evidence_text, context)
    return score


def _text_match_score(left: str, right: str) -> int:
    return len(_search_grams(left) & _search_grams(right))


def _search_grams(text: str) -> set[str]:
    grams = {token.lower() for token in re.findall(r"[A-Za-z0-9]+", text)}
    for sequence in re.findall(r"[\u3400-\u9fff]+", text):
        if len(sequence) == 1:
            grams.add(sequence)
        else:
            grams.update(sequence[index : index + 2] for index in range(len(sequence) - 1))
    return grams


def _preview_from_evidence_paths(
    document: dict[str, object],
    parsed_path: Path,
    base_path: Path,
    evidence_text: str,
    source_type: str,
    question: str | None = None,
) -> tuple[str | None, str | None]:
    raw_matches = [*HTML_IMAGE_RE.findall(evidence_text)]
    raw_matches.extend(re.findall(r"images/[^\s\"'<>，。；)）]+", evidence_text))
    raw_matches = _rank_image_paths_by_question(evidence_text, raw_matches, question)
    for raw_match in raw_matches:
        preview = _asset_preview(document, parsed_path, base_path / raw_match, source_type)
        if preview[0]:
            return preview
    return None, None


def _rank_image_paths_by_question(
    evidence_text: str,
    raw_paths: list[str],
    question: str | None,
) -> list[str]:
    paths = list(dict.fromkeys(raw_paths))
    if not question or len(paths) <= 1:
        return paths
    question_terms = _search_grams(question)
    if not question_terms:
        return paths

    def score(path: str) -> tuple[int, int]:
        index = evidence_text.find(path)
        if index < 0:
            return (0, 0)
        context = evidence_text[max(0, index - 80) : index]
        context_terms = _search_grams(context)
        return (len(question_terms & context_terms), index * -1)

    return sorted(paths, key=score, reverse=True)


def _asset_preview(
    document: dict[str, object],
    parsed_path: Path,
    raw_path: Path,
    source_type: str,
) -> tuple[str | None, str | None]:
    resolved = raw_path.resolve()
    try:
        relative = resolved.relative_to(parsed_path.resolve()).as_posix()
    except ValueError:
        return None, None
    if not resolved.exists() or not resolved.is_file():
        return None, None
    label = "表格截图" if "table" in source_type else "图片内容"
    document_id = quote(str(document["id"]), safe="")
    asset_path = quote(relative, safe="/")
    return f"/api/documents/{document_id}/asset?path={asset_path}", label


def _item_image_paths(item: dict[str, object]) -> list[Path]:
    paths: list[Path] = []
    for key in ("img_path", "image_path", "path", "src"):
        value = item.get(key)
        if isinstance(value, str) and value:
            paths.append(Path(value))
    table_body = item.get("table_body")
    if isinstance(table_body, str):
        paths.extend(Path(match) for match in HTML_IMAGE_RE.findall(table_body))
    return paths


class RagService:
    def __init__(
        self,
        settings: Settings,
        repository: Repository,
        ingestion: IngestionManager,
        retriever: AgenticRetriever,
        chat_provider: ChatProvider,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.ingestion = ingestion
        self.retriever = retriever
        self.chat_provider = chat_provider

    async def upload(self, upload: UploadFile) -> tuple[dict[str, object], str, bool]:
        filename = Path(upload.filename or "").name
        if not filename.lower().endswith(".pdf"):
            raise ValueError("当前仅支持 PDF 文件")
        document_id = str(uuid4())
        target = self.settings.data_dir / "uploads" / f"{document_id}.pdf"
        digest = hashlib.sha256()
        file_size = 0
        max_bytes = self.settings.max_file_size_mb * 1024 * 1024
        with target.open("wb") as stream:
            while chunk := await upload.read(1024 * 1024):
                file_size += len(chunk)
                if file_size > max_bytes:
                    target.unlink(missing_ok=True)
                    raise ValueError(f"文件超过当前 {self.settings.max_file_size_mb} MB 上限")
                digest.update(chunk)
                stream.write(chunk)
        sha256 = digest.hexdigest()
        duplicate = self.repository.get_document_by_hash(sha256)
        if duplicate:
            target.unlink(missing_ok=True)
            duplicate_id = str(duplicate["id"])
            latest_job = self.repository.latest_job_for_document(duplicate_id)
            if latest_job and latest_job["status"] in {"QUEUED", "RUNNING"}:
                job_id = str(latest_job["id"])
            elif str(duplicate["status"]) == "READY" and latest_job is not None:
                job_id = str(latest_job["id"])
            else:
                self.repository.update_document(
                    duplicate_id,
                    status="QUEUED",
                    page_count=0,
                    needs_ocr=0,
                    parser_name=None,
                    parsed_path=None,
                    error=None,
                )
                job_id = str(uuid4())
                self.repository.create_job(job_id, duplicate_id)
                self.ingestion.submit(job_id)
                duplicate = self.repository.get_document(duplicate_id) or duplicate
            return duplicate, job_id, True

        title, standard_no, version = infer_metadata(filename)
        document = self.repository.create_document(
            document_id=document_id,
            filename=filename,
            stored_path=target,
            sha256=sha256,
            title=title,
            standard_no=standard_no,
            version=version,
            file_size=file_size,
        )
        job_id = str(uuid4())
        self.repository.create_job(job_id, document_id)
        self.ingestion.submit(job_id)
        return document, job_id, False

    async def reparse_document(
        self,
        document_id: str,
        upload: UploadFile,
    ) -> tuple[dict[str, object], str]:
        document = self.repository.get_document(document_id)
        if document is None:
            raise LookupError("document not found")
        active_job = self.repository.active_job_for_document(document_id)
        if active_job is not None:
            raise RuntimeError(f"document already has an active job: {active_job['id']}")

        filename = Path(upload.filename or "").name
        if not filename.lower().endswith(".pdf"):
            raise ValueError("当前仅支持 PDF 文件")

        digest = hashlib.sha256()
        chunks: list[bytes] = []
        file_size = 0
        max_bytes = self.settings.max_file_size_mb * 1024 * 1024
        while chunk := await upload.read(1024 * 1024):
            file_size += len(chunk)
            if file_size > max_bytes:
                raise ValueError(f"文件超过当前 {self.settings.max_file_size_mb} MB 上限")
            digest.update(chunk)
            chunks.append(chunk)

        sha256 = digest.hexdigest()
        duplicate = self.repository.get_document_by_hash(sha256)
        if duplicate and str(duplicate["id"]) != document_id:
            raise ValueError("PDF already exists in another document")

        target = Path(str(document["stored_path"]))
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("wb") as stream:
            for chunk in chunks:
                stream.write(chunk)

        title, standard_no, version = infer_metadata(filename)
        update_values: dict[str, object] = {
            "filename": filename,
            "sha256": sha256,
            "file_size": file_size,
            "status": "QUEUED",
            "page_count": 0,
            "needs_ocr": 0,
            "parser_name": None,
            "parsed_path": None,
            "error": None,
        }
        if title:
            update_values["title"] = title
        if standard_no:
            update_values["standard_no"] = standard_no
        if version:
            update_values["version"] = version
        try:
            self.repository.update_document(document_id, **update_values)
        except IntegrityError as exc:
            raise ValueError("PDF already exists in another document") from exc

        job_id = str(uuid4())
        self.repository.create_job(job_id, document_id)
        self.ingestion.submit(job_id)
        updated = self.repository.get_document(document_id)
        if updated is None:
            raise RuntimeError("document update failed")
        return updated, job_id

    def chat(
        self,
        question: str,
        document_ids: list[str] | None,
    ) -> ChatResponse:
        context = self.chat_context(question, document_ids)
        answer = self.chat_provider.answer(question, context.hits, context.query_type).strip()
        return self.chat_response_from_context(context, answer)

    def chat_context(
        self,
        question: str,
        document_ids: list[str] | None,
    ) -> ChatContext:
        trace_id = str(uuid4())
        agent_run = self.retriever.run(question, document_ids)
        kind = agent_run.kind
        hits = agent_run.hits
        documents = {
            hit.document_id: self.repository.get_document(hit.document_id)
            for hit in hits[: self.settings.answer_max_citations]
        }
        citations = [
            self._citation(index, hit, documents.get(hit.document_id), question)
            for index, hit in enumerate(
                hits[: self.settings.answer_max_citations],
                1,
            )
        ]
        document_count = len({citation.document_id for citation in citations})
        if not citations:
            evidence_status = "insufficient"
        elif kind == "comparison" and document_count < 2:
            evidence_status = "partial"
        else:
            evidence_status = "sufficient"
        return ChatContext(
            trace_id=trace_id,
            question=question,
            query_type=kind,
            hits=hits,
            citations=citations,
            evidence_status=evidence_status,
            used_external_llm=self.chat_provider.external,
            retrieval_timings={
                "planner_ms": agent_run.planner_ms,
                "recall_ms": agent_run.recall_ms,
                "rerank_ms": agent_run.rerank_ms,
            },
        )

    def chat_response_from_context(
        self,
        context: ChatContext,
        answer: str,
    ) -> ChatResponse:
        return ChatResponse(
            answer=answer,
            citations=context.citations,
            evidence_status=context.evidence_status,
            query_type=context.query_type,
            used_external_llm=context.used_external_llm,
            trace_id=context.trace_id,
        )

    def _citation(
        self,
        index: int,
        hit,
        document: dict[str, object] | None,
        question: str,
    ) -> Citation:
        preview_image_url, preview_label = preview_from_parsed(
            document,
            hit.page_start,
            hit.content_type,
            hit.text,
            question,
        )
        return Citation(
            index=index,
            chunk_id=hit.chunk_id,
            document_id=hit.document_id,
            document_title=hit.document_title,
            standard_no=hit.standard_no,
            version=hit.version,
            clause_no=hit.clause_no,
            chapter_path=hit.chapter_path,
            pdf_page=hit.page_start,
            printed_page=hit.printed_page,
            quote=hit.text[:420],
            score=round(hit.score, 6),
            source_type=hit.content_type,
            preview_image_url=preview_image_url,
            preview_label=preview_label,
        )
