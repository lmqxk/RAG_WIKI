"""PDF 解析模块：默认使用 OpenDataLab PDF-Extract-Kit pipeline，并保留表格、图片和页码信息。"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from html.parser import HTMLParser
from pathlib import Path

import fitz
import numpy as np

from .config import Settings
from .domain import PageBlock, ParsedDocument

ProgressCallback = Callable[[float, str], None]
PRINTED_PAGE_RE = re.compile(r"(?:^|[·•\-\s])(\d{1,4})(?:[·•\-\s]|$)")


class ParsingError(RuntimeError):
    pass


IMAGE_PATH_RE = re.compile(r"""<img[^>]+src=["']([^"']+)["']""", re.IGNORECASE)


def _clean_cell_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _caption_before_image(text: str) -> str:
    text = _clean_cell_text(text)
    text = re.sub(r"^(?:\d+(?:\.\d+)?\s*)+", "", text).strip()
    text = re.sub(r"(?:\d+(?:\.\d+)?\s*)+$", "", text).strip()
    parts = re.split(r"[;；。]\s*", text)
    return parts[-1].strip() if parts else text


def _positive_int(value: object, default: int = 1) -> int:
    try:
        parsed = int(value or default)
    except (TypeError, ValueError):
        return default
    return max(1, parsed)


class _ImageTableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[dict[str, object]]] = []
        self._current_row: list[dict[str, object]] | None = None
        self._current_cell: dict[str, object] | None = None
        self._text_parts: list[str] = []
        self._last_image_text_index = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tr":
            self._current_row = []
            return
        if tag in {"td", "th"} and self._current_row is not None:
            values = dict(attrs)
            self._text_parts = []
            self._last_image_text_index = 0
            self._current_cell = {
                "text": "",
                "rowspan": _positive_int(values.get("rowspan")),
                "colspan": _positive_int(values.get("colspan")),
                "images": [],
            }
            return
        if tag == "img" and self._current_cell is not None:
            src = dict(attrs).get("src")
            if not src:
                return
            text_before = "".join(self._text_parts[self._last_image_text_index :])
            images = self._current_cell["images"]
            assert isinstance(images, list)
            images.append(
                {
                    "path": src,
                    "caption": _caption_before_image(text_before),
                    "order": len(images) + 1,
                }
            )
            self._last_image_text_index = len(self._text_parts)

    def handle_data(self, data: str) -> None:
        if self._current_cell is not None:
            self._text_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"td", "th"} and self._current_row is not None and self._current_cell is not None:
            self._current_cell["text"] = _clean_cell_text("".join(self._text_parts))
            self._current_row.append(self._current_cell)
            self._current_cell = None
            return
        if tag == "tr" and self._current_row is not None:
            self.rows.append(self._current_row)
            self._current_row = None


def _expand_table_cells(rows: list[list[dict[str, object]]]) -> list[list[dict[str, object]]]:
    active: dict[int, tuple[int, dict[str, object]]] = {}
    expanded: list[list[dict[str, object]]] = []
    for raw_row in rows:
        row: list[dict[str, object]] = []
        column = 0

        def consume_active(target_row: list[dict[str, object]]) -> None:
            nonlocal column
            while column in active:
                remaining, cell = active.pop(column)
                target_row.append(cell)
                if remaining > 1:
                    active[column] = (remaining - 1, cell)
                column += 1

        consume_active(row)
        for cell in raw_row:
            consume_active(row)
            rowspan = _positive_int(cell.get("rowspan"))
            colspan = _positive_int(cell.get("colspan"))
            for offset in range(colspan):
                row.append(cell)
                if rowspan > 1:
                    active[column + offset] = (rowspan - 1, cell)
            column += colspan
        consume_active(row)
        if row:
            expanded.append(row)
    return expanded


def _row_context(row: list[dict[str, object]], header: list[str]) -> str:
    for index, cell in enumerate(row):
        text = _clean_cell_text(str(cell.get("text", "")))
        heading = header[index] if index < len(header) else ""
        if text and ("构件名称" in heading or index == 0):
            return text
    return _clean_cell_text(str(row[0].get("text", ""))) if row else ""


def _table_images(html: str) -> list[dict[str, object]]:
    if not IMAGE_PATH_RE.search(html):
        return []
    parser = _ImageTableParser()
    parser.feed(html)
    rows = _expand_table_cells(parser.rows)
    if len(rows) <= 1:
        return []
    header = [_clean_cell_text(str(cell.get("text", ""))) for cell in rows[0]]
    result: list[dict[str, object]] = []
    for row in rows[1:]:
        row_context = _row_context(row, header)
        seen_cells: set[int] = set()
        for column_index, cell in enumerate(row):
            cell_id = id(cell)
            if cell_id in seen_cells:
                continue
            seen_cells.add(cell_id)
            images = cell.get("images", [])
            if not isinstance(images, list) or not images:
                continue
            column_context = header[column_index] if column_index < len(header) else ""
            cell_text = _clean_cell_text(str(cell.get("text", "")))
            for image in images:
                if not isinstance(image, dict):
                    continue
                result.append(
                    {
                        "path": str(image.get("path", "")),
                        "caption": str(image.get("caption", "") or cell_text),
                        "row_context": row_context,
                        "column_context": column_context,
                        "cell_text": cell_text,
                        "order": len(result) + 1,
                    }
                )
    return [image for image in result if image["path"]]


def inspect_pdf(path: Path, max_pages: int) -> tuple[int, bool]:
    with fitz.open(path) as document:
        page_count = document.page_count
        if page_count > max_pages:
            raise ParsingError(f"PDF 共 {page_count} 页，超过当前上限 {max_pages} 页")
        sample_indices = sorted(
            {
                0,
                min(1, page_count - 1),
                page_count // 4,
                page_count // 2,
                (page_count * 3) // 4,
                page_count - 1,
            }
        )
        text_pages = 0
        for index in sample_indices:
            if len(document[index].get_text("text").strip()) >= 30:
                text_pages += 1
    needs_ocr = text_pages < max(1, len(sample_indices) // 2)
    return page_count, needs_ocr


def _printed_page(text: str) -> str | None:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for line in reversed(lines[-4:]):
        match = PRINTED_PAGE_RE.search(line)
        if match and len(line) <= 20:
            return match.group(1)
    return None


class NativePdfParser:
    name = "pymupdf"

    def parse(self, path: Path, progress: ProgressCallback) -> ParsedDocument:
        blocks: list[PageBlock] = []
        markdown_pages: list[str] = []
        with fitz.open(path) as document:
            total = document.page_count
            for index, page in enumerate(document):
                raw_text = page.get_text("text")
                printed_page = _printed_page(raw_text)
                page_blocks: list[str] = []
                for block in page.get_text("blocks", sort=True):
                    text = " ".join(str(block[4]).split())
                    if not text:
                        continue
                    block_type = "image" if int(block[6]) == 1 else "text"
                    blocks.append(
                        PageBlock(
                            page=index + 1,
                            text=text,
                            block_type=block_type,
                            bbox=[float(value) for value in block[:4]],
                            printed_page=printed_page,
                        )
                    )
                    if block_type == "text":
                        page_blocks.append(text)
                markdown_pages.append(
                    f"<!-- PDF_PAGE:{index + 1} -->\n\n" + "\n\n".join(page_blocks)
                )
                progress(
                    10 + ((index + 1) / max(total, 1)) * 40,
                    f"提取文本 {index + 1}/{total} 页",
                )
        return ParsedDocument(
            pages=total,
            blocks=blocks,
            markdown="\n\n".join(markdown_pages),
            parser_name=self.name,
            needs_ocr=False,
        )


class RapidOcrParser:
    """逐页 OCR，内存占用只与单页渲染尺寸相关。"""

    name = "rapidocr"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @staticmethod
    def available() -> bool:
        try:
            from rapidocr import RapidOCR  # noqa: F401
        except ImportError:
            return False
        return True

    def parse(self, path: Path, progress: ProgressCallback) -> ParsedDocument:
        try:
            from rapidocr import RapidOCR
        except ImportError as exc:
            raise ParsingError(
                "扫描 PDF 需要 RapidOCR，请安装项目可选依赖 ocr。"
            ) from exc

        progress(10, "初始化 RapidOCR")
        engine = RapidOCR()
        blocks: list[PageBlock] = []
        markdown_pages: list[str] = []
        matrix = fitz.Matrix(
            self.settings.ocr_render_dpi / 72,
            self.settings.ocr_render_dpi / 72,
        )
        with fitz.open(path) as document:
            total = document.page_count
            for index, page in enumerate(document):
                pixmap = page.get_pixmap(matrix=matrix, alpha=False)
                image = np.frombuffer(pixmap.samples, dtype=np.uint8).reshape(
                    pixmap.height,
                    pixmap.width,
                    pixmap.n,
                )
                result = engine(image)
                texts = list(result.txts or ())
                scores = list(result.scores or ())
                boxes = list(result.boxes) if result.boxes is not None else []
                ordered: list[tuple[float, float, str, float, list[float]]] = []
                for line_index, text in enumerate(texts):
                    clean_text = " ".join(str(text).split())
                    if not clean_text:
                        continue
                    box = boxes[line_index] if line_index < len(boxes) else None
                    if box is None:
                        bbox: list[float] = []
                        y_value = float(line_index)
                        x_value = 0.0
                    else:
                        box_array = np.asarray(box, dtype=float)
                        bbox = [
                            float(box_array[:, 0].min()),
                            float(box_array[:, 1].min()),
                            float(box_array[:, 0].max()),
                            float(box_array[:, 1].max()),
                        ]
                        x_value, y_value = bbox[0], bbox[1]
                    score = float(scores[line_index]) if line_index < len(scores) else 0.0
                    ordered.append((y_value, x_value, clean_text, score, bbox))

                ordered.sort(key=lambda item: (round(item[0] / 8), item[1]))
                page_text = "\n".join(item[2] for item in ordered)
                printed_page = _printed_page(page_text)
                for _, _, text, score, bbox in ordered:
                    if score < 0.35:
                        continue
                    blocks.append(
                        PageBlock(
                            page=index + 1,
                            text=text,
                            block_type="text",
                            bbox=bbox,
                            printed_page=printed_page,
                        )
                    )
                markdown_pages.append(
                    f"<!-- PDF_PAGE:{index + 1} -->\n\n"
                    + "\n\n".join(item[2] for item in ordered if item[3] >= 0.35)
                )
                progress(
                    10 + ((index + 1) / max(total, 1)) * 45,
                    f"RapidOCR 识别 {index + 1}/{total} 页",
                )

        return ParsedDocument(
            pages=total,
            blocks=blocks,
            markdown="\n\n".join(markdown_pages),
            parser_name=self.name,
            needs_ocr=True,
        )


PIPELINE_BACKENDS = {
    "pdf-extract-kit": "pipeline",
    "pdf-extract-kit-1.0": "pipeline",
    "opendatalab-pdf-extract-kit": "pipeline",
    "opendatalab-pdf-extract-kit-1.0": "pipeline",
    "mineru": "vlm-engine",
    "mineru-vlm": "vlm-engine",
    "vlm": "vlm-engine",
    "vlm-engine": "vlm-engine",
    "hybrid": "hybrid-engine",
    "hybrid-engine": "hybrid-engine",
}


class DocumentPipelineParser:
    name = "opendatalab-pdf-extract-kit"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def command(self) -> str | None:
        if self.settings.mineru_command:
            return self.settings.mineru_command
        discovered = shutil.which("mineru")
        if discovered:
            return discovered
        candidate = Path(__file__).resolve().parents[2] / ".venv" / "Scripts" / "mineru.exe"
        return str(candidate) if candidate.exists() else None

    def available(self) -> bool:
        return self.command() is not None

    def pipeline(self) -> str:
        return self.settings.document_pipeline.strip().lower()

    def cli_backend(self) -> str:
        pipeline = self.pipeline()
        if pipeline in PIPELINE_BACKENDS:
            return PIPELINE_BACKENDS[pipeline]
        if self.settings.mineru_backend:
            return self.settings.mineru_backend
        return pipeline

    def parser_name(self) -> str:
        backend = self.cli_backend().lower()
        if backend == "pipeline":
            return self.name
        return f"mineru-{backend}"

    def parser_label(self) -> str:
        backend = self.cli_backend().lower()
        if backend == "pipeline":
            return "OpenDataLab PDF-Extract-Kit 1.0"
        if backend == "vlm-engine":
            return "MinerU VLM"
        return f"MinerU {backend}"

    def parse(
        self,
        path: Path,
        output_dir: Path,
        progress: ProgressCallback,
    ) -> ParsedDocument:
        command = self.command()
        parser_label = self.parser_label()
        if command is None:
            raise ParsingError(
                "该 PDF 没有文本层，需要 OpenDataLab PDF-Extract-Kit。请先安装项目可选依赖或配置 "
                "RAG_MINERU_COMMAND。"
            )
        output_dir.mkdir(parents=True, exist_ok=True)
        arguments = [
            command,
            "-p",
            str(path),
            "-o",
            str(output_dir),
            "-b",
            self.cli_backend(),
            "-m",
            self.settings.mineru_method,
            "-l",
            self.settings.mineru_ocr_lang,
        ]
        progress(12, f"启动 {parser_label} 文档解析")
        process = subprocess.Popen(
            arguments,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=self._environment(output_dir),
        )
        timed_out = False

        timeout_seconds = max(1, int(self.settings.document_parse_timeout_seconds))
        output_lines: list[str] = []

        def read_output() -> None:
            assert process.stdout is not None
            for line in process.stdout:
                output_lines.append(line.rstrip())
                percent_match = re.search(r"(\d{1,3})%", line)
                if percent_match:
                    percent = min(int(percent_match.group(1)), 100)
                    progress(12 + percent * 0.43, f"{parser_label} 解析中 {percent}%")

        reader = threading.Thread(target=read_output, daemon=True)
        reader.start()
        started_at = time.monotonic()
        return_code: int | None = None
        while True:
            return_code = process.poll()
            if return_code is not None:
                break
            if time.monotonic() - started_at >= timeout_seconds:
                timed_out = True
                self._kill_process_tree(process)
                break
            time.sleep(0.5)
        reader.join(timeout=5)
        if return_code is None:
            return_code = process.wait()
        if timed_out:
            if self._has_output(output_dir):
                progress(57, f"{parser_label} 子进程超时，读取已生成的结构化结果")
                return self._load_output(output_dir)
            raise ParsingError(f"{parser_label} 解析超时，已终止子进程（{timeout_seconds} 秒）。")
        if return_code != 0:
            tail = "\n".join(output_lines[-20:])
            raise ParsingError(f"{parser_label} 解析失败（退出码 {return_code}）：\n{tail}")
        progress(57, f"读取 {parser_label} 结构化结果")
        return self._load_output(output_dir)

    def _environment(self, output_dir: Path) -> dict[str, str]:
        environment = os.environ.copy()
        model_root = self.settings.mineru_model_dir
        temp_root = self.settings.data_dir / "tmp" / "mineru"
        temp_root.mkdir(parents=True, exist_ok=True)
        environment.update(
            {
                "MINERU_MODEL_SOURCE": self.settings.mineru_model_source,
                "MINERU_TOOLS_CONFIG_JSON": str(
                    Path(__file__).resolve().parents[2] / "config" / "mineru.json"
                ),
                "MINERU_API_OUTPUT_ROOT": str(output_dir),
                "MODELSCOPE_CACHE": str(model_root / "modelscope"),
                "MODELSCOPE_HOME": str(model_root / "modelscope-config"),
                "HF_HOME": str(model_root / "huggingface"),
                "HUGGINGFACE_HUB_CACHE": str(model_root / "huggingface" / "hub"),
                "TORCH_HOME": str(model_root / "torch"),
                "XDG_CACHE_HOME": str(model_root / "cache"),
                "TEMP": str(temp_root),
                "TMP": str(temp_root),
                "MINERU_PROCESSING_WINDOW_SIZE": "8",
                "MINERU_API_MAX_CONCURRENT_REQUESTS": "1",
            }
        )
        return environment

    def _load_output(self, output_dir: Path) -> ParsedDocument:
        content_candidates = list(output_dir.rglob("*content_list.json"))
        markdown_candidates = list(output_dir.rglob("*.md"))
        if not content_candidates:
            raise ParsingError(f"{self.parser_label()} 已结束，但未找到 content_list.json")
        content_path = max(content_candidates, key=lambda path: path.stat().st_mtime)
        content = json.loads(content_path.read_text(encoding="utf-8"))
        blocks: list[PageBlock] = []
        max_page = 0
        for item in content:
            page = int(item.get("page_idx", 0)) + 1
            max_page = max(max_page, page)
            block_type = str(item.get("type", "text"))
            text = self._item_text(item)
            if not text:
                continue
            blocks.append(
                PageBlock(
                    page=page,
                    text=text,
                    block_type=block_type,
                    bbox=[float(value) for value in item.get("bbox", [])],
                    level=item.get("text_level"),
                    images=self._item_images(item),
                )
            )
        markdown = ""
        if markdown_candidates:
            markdown_path = max(markdown_candidates, key=lambda path: path.stat().st_mtime)
            markdown = markdown_path.read_text(encoding="utf-8", errors="replace")
        return ParsedDocument(
            pages=max_page,
            blocks=blocks,
            markdown=markdown,
            parser_name=self.parser_name(),
            needs_ocr=True,
        )

    def _has_output(self, output_dir: Path) -> bool:
        return any(output_dir.rglob("*content_list.json"))

    def _kill_process_tree(self, process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            return
        process.kill()

    def _item_text(self, item: dict[str, object]) -> str:
        parts: list[str] = []

        def append(value: object, prefix: str | None = None) -> None:
            if value is None:
                return
            if isinstance(value, list):
                text = " ".join(str(part).strip() for part in value if str(part).strip())
            else:
                text = str(value).strip()
            if not text:
                return
            parts.append(f"{prefix}{text}" if prefix else text)

        block_type = str(item.get("type", "text")).lower()
        append(item.get("text"))
        append(item.get("table_caption"), "表格标题：")
        append(item.get("table_body"), "表格内容：")
        append(item.get("table_footnote"), "表格注释：")
        append(item.get("img_caption"), "图片标题：")
        append(item.get("image_caption"), "图片标题：")
        append(item.get("img_footnote"), "图片注释：")
        append(item.get("image_footnote"), "图片注释：")

        image_path = (
            item.get("img_path")
            or item.get("image_path")
            or item.get("path")
            or item.get("src")
        )
        if image_path:
            append(image_path, "图片文件：")
        if not parts and block_type in {"image", "img", "figure"}:
            parts.append("图片：该位置包含图片内容")
        if not parts and "table" in block_type:
            parts.append("表格：该位置包含表格内容")
        return " ".join(parts).strip()

    def _item_images(self, item: dict[str, object]) -> list[dict[str, object]]:
        table_body = item.get("table_body")
        if isinstance(table_body, str):
            table_images = _table_images(table_body)
            if table_images:
                return table_images
        image_path = (
            item.get("img_path")
            or item.get("image_path")
            or item.get("path")
            or item.get("src")
        )
        if isinstance(image_path, str) and image_path:
            caption = item.get("img_caption") or item.get("image_caption") or ""
            if isinstance(caption, list):
                caption = " ".join(str(part).strip() for part in caption if str(part).strip())
            return [
                {
                    "path": image_path,
                    "caption": str(caption),
                    "row_context": "",
                    "column_context": "",
                    "cell_text": "",
                    "order": 1,
                }
            ]
        return []


class DocumentParser:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.native = NativePdfParser()
        self.rapidocr = RapidOcrParser(settings)
        self.mineru = DocumentPipelineParser(settings)

    def available(self) -> bool:
        pipeline = self.settings.document_pipeline.strip().lower()
        if pipeline in {"pymupdf", "native"}:
            return True
        if pipeline == "rapidocr":
            return self.rapidocr.available()
        return self.mineru.available()

    def parse(
        self,
        path: Path,
        output_dir: Path,
        progress: ProgressCallback,
    ) -> ParsedDocument:
        page_count, needs_ocr = inspect_pdf(path, self.settings.max_pdf_pages)
        progress(8, f"PDF 检查完成，共 {page_count} 页")
        pipeline = self.settings.document_pipeline.strip().lower()
        if pipeline in {"pymupdf", "native"}:
            return self.native.parse(path, progress)
        if pipeline == "rapidocr":
            return self.rapidocr.parse(path, progress)
        scan_parser = self.settings.scan_parser.strip().lower()
        if pipeline in PIPELINE_BACKENDS or scan_parser in PIPELINE_BACKENDS:
            if self.mineru.available():
                return self.mineru.parse(path, output_dir, progress)
            if needs_ocr:
                raise ParsingError(
                    "当前默认使用 OpenDataLab PDF-Extract-Kit 解析，但未找到解析命令。"
                )
        if needs_ocr:
            return self.rapidocr.parse(path, progress)
        return self.native.parse(path, progress)


MinerUParser = DocumentPipelineParser
