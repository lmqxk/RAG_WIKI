"""表格 PDF 解析对比测试：Paddle VL vs MinerU。

测试目标：
- 分别使用 Paddle VL (PPStructureV3 / PaddleOCRVL) 和 MinerU 解析同一份表格 PDF
- 对比两者的表格识别质量、输出格式、性能差异
- 输出结构化 JSON 报告以便后续分析

默认测试文件：test/table3.pdf
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import warnings
from pathlib import Path

import fitz

from backend.chunking import build_chunks
from backend.config import Settings
from backend.parser import MinerUParser

# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------


def write_text(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8", errors="replace")


def write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def render_first_page(pdf_path: Path, output_dir: Path) -> Path:
    """渲染 PDF 第一页为图片，便于肉眼对照。"""
    image_path = output_dir / "page-1.png"
    with fitz.open(pdf_path) as doc:
        page = doc[0]
        pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
        pix.save(image_path)
    return image_path


# ---------------------------------------------------------------------------
# Paddle VL 表格解析
# ---------------------------------------------------------------------------


def _init_paddlevl_ppstructure() -> object | None:
    """初始化 PPStructureV3（含表格识别）。

    返回 PPStructureV3 实例，如果环境不可用则返回 None 并打印原因。
    """
    try:
        # 尝试 CPU 模式（GPU DLL 缺失时自动感知）
        os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")
        warnings.filterwarnings("ignore")
        from paddleocr import PPStructureV3

        engine = PPStructureV3(
            use_table_recognition=True,
            lang="ch",
        )
        return engine
    except OSError as exc:
        msg = str(exc)
        if "cudnn" in msg.lower() or "找不到" in msg or "Error loading" in msg:
            print(
                "[Paddle VL] GPU 环境不完整（缺少 CUDA/cuDNN DLL），"
                "请安装 paddlepaddle (CPU) 或配置 CUDA 环境。"
            )
            print(f"  详细错误: {msg[:200]}")
        else:
            print(f"[Paddle VL] 初始化失败 (OSError): {msg[:200]}")
        return None
    except ImportError as exc:
        print(f"[Paddle VL] 导入失败: {exc}")
        return None
    except Exception as exc:
        print(f"[Paddle VL] 初始化失败: {type(exc).__name__}: {exc}")
        return None


def _init_paddlevl_table_pipeline() -> object | None:
    """初始化 TableRecognitionPipelineV2（纯表格识别管线）。

    返回 TableRecognitionPipelineV2 实例，不可用则返回 None。
    """
    try:
        os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")
        warnings.filterwarnings("ignore")
        from paddleocr import TableRecognitionPipelineV2

        engine = TableRecognitionPipelineV2(
            use_layout_detection=True,
            use_ocr_model=True,
        )
        return engine
    except OSError as exc:
        print(f"[Paddle VL TablePipeline] 环境不可用 (OSError): {exc}")
        return None
    except ImportError as exc:
        print(f"[Paddle VL TablePipeline] 导入失败: {exc}")
        return None
    except Exception as exc:
        print(f"[Paddle VL TablePipeline] 初始化失败: {type(exc).__name__}: {exc}")
        return None


def _init_paddlevl_ocrvl() -> object | None:
    """初始化 PaddleOCRVL（VLM 文档理解）。

    返回 PaddleOCRVL 实例，不可用则返回 None。
    """
    try:
        os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")
        warnings.filterwarnings("ignore")
        from paddleocr import PaddleOCRVL

        engine = PaddleOCRVL(
            use_layout_detection=True,
            lang="ch",
        )
        return engine
    except OSError as exc:
        print(f"[PaddleOCRVL] 环境不可用 (OSError): {exc}")
        return None
    except ImportError as exc:
        print(f"[PaddleOCRVL] 导入失败: {exc}")
        return None
    except Exception as exc:
        print(f"[PaddleOCRVL] 初始化失败: {type(exc).__name__}: {exc}")
        return None


def _extract_paddle_result(result: list, method: str) -> dict:
    """将 Paddle VL 解析结果提取为统一格式。

    PPStructureV3 / PaddleOCRVL 返回的每个条目通常包含:
      - type: 块类型 (text / table / image / ...)
      - text / rec_texts: OCR 文本
      - bbox: 边界框
      - table_html: 表格 HTML (如果类型是 table)
      - markdown: 结构化 Markdown 片段
      - img: 可视化图片 (numpy array)
    """
    extracted: dict = {
        "method": method,
        "total_items": len(result),
        "table_items": [],
        "text_items": [],
        "image_items": [],
        "other_items": [],
        "markdown": "",
    }

    markdown_parts: list[str] = []

    for idx, item in enumerate(result):
        item_type = ""
        item_bbox = None
        item_text = ""
        item_html = ""
        item_md = ""

        # 尝试多种属性访问方式
        if hasattr(item, "__dict__"):
            d = item.__dict__
            item_type = str(d.get("type", "")).lower()
            item_bbox = d.get("bbox")
            item_text = d.get("text", "") or ""
            item_html = d.get("table_html", "") or d.get("html", "") or ""
            item_md = d.get("markdown", "") or d.get("md", "") or ""
            # rec_texts 可能是列表，合并
            rec_texts = d.get("rec_texts", [])
            if rec_texts and not item_text:
                item_text = " ".join(
                    str(t) for t in (rec_texts if isinstance(rec_texts, list) else [rec_texts])
                )
        elif isinstance(item, dict):
            item_type = str(item.get("type", "")).lower()
            item_bbox = item.get("bbox")
            item_text = item.get("text", "") or ""
            item_html = item.get("table_html", "") or item.get("html", "") or ""
            item_md = item.get("markdown", "") or item.get("md", "") or ""

        base_info = {
            "index": idx,
            "type": item_type or "unknown",
            "bbox": item_bbox,
            "text_preview": (str(item_text)[:300] if item_text else ""),
        }

        if "table" in item_type:
            table_info = {
                **base_info,
                "table_html": item_html,
                "table_html_length": len(item_html) if item_html else 0,
            }
            extracted["table_items"].append(table_info)
            markdown_parts.append(item_md or item_html or item_text or "")
        elif item_type in ("text", "paragraph", "title", "header", "footer"):
            extracted["text_items"].append(base_info)
            markdown_parts.append(item_md or str(item_text or ""))
        elif item_type in ("image", "img", "figure", "chart"):
            extracted["image_items"].append(base_info)
            markdown_parts.append(item_md or "")
        else:
            extracted["other_items"].append(base_info)
            if item_text:
                markdown_parts.append(str(item_text))

    extracted["markdown"] = "\n\n".join(p for p in markdown_parts if p.strip())
    extracted["table_count"] = len(extracted["table_items"])
    extracted["text_block_count"] = len(extracted["text_items"])
    extracted["image_count"] = len(extracted["image_items"])

    return extracted


def test_paddle_vl(
    pdf_path: Path,
    output_dir: Path,
    *,
    use_ppstructure: bool = True,
    use_table_pipeline: bool = True,
    use_ocrvl: bool = False,
) -> dict:
    """使用 Paddle VL 方法解析表格 PDF。

    参数
    ----
    use_ppstructure : 使用 PPStructureV3（综合文档结构分析，含表格）
    use_table_pipeline : 使用 TableRecognitionPipelineV2（纯表格管线）
    use_ocrvl : 使用 PaddleOCRVL（VLM 文档理解）

    返回
    ----
    {"success": bool, "results": [...], "errors": [...], "summary": {...}}
    """
    results: list[dict] = []
    errors: list[str] = []

    # --- PPStructureV3 ---
    if use_ppstructure:
        print("=" * 60)
        print("[Paddle VL] 测试 PPStructureV3 ...")
        t0 = time.perf_counter()
        engine = _init_paddlevl_ppstructure()
        if engine is not None:
            try:
                raw = engine.predict(str(pdf_path))
                elapsed = time.perf_counter() - t0
                info = _extract_paddle_result(raw, "PPStructureV3")
                info["elapsed_seconds"] = round(elapsed, 2)
                write_json(output_dir / "paddlevl-ppstructure.json", info)

                # 额外保存 markdown + HTML 表格
                write_text(output_dir / "paddlevl-ppstructure.md", info["markdown"])
                for i, tbl in enumerate(info["table_items"]):
                    if tbl.get("table_html"):
                        write_text(
                            output_dir / f"paddlevl-ppstructure-table-{i + 1}.html",
                            f"<!DOCTYPE html><html><head><meta charset='utf-8'>"
                            f"<style>table{{border-collapse:collapse;width:100%}}"
                            f"td,th{{border:1px solid #ccc;padding:6px;text-align:left}}"
                            f"</style></head><body>{tbl['table_html']}</body></html>",
                        )

                print(
                    f"  PPStructureV3 完成: {elapsed:.1f}s, "
                    f"表格={info['table_count']}, "
                    f"文字块={info['text_block_count']}, "
                    f"图片={info['image_count']}"
                )
                results.append(
                    {
                        "method": "PPStructureV3",
                        "status": "ok",
                        "elapsed_seconds": round(elapsed, 2),
                        "table_count": info["table_count"],
                        "text_block_count": info["text_block_count"],
                        "image_count": info["image_count"],
                        "markdown_length": len(info["markdown"]),
                        "output_file": str(output_dir / "paddlevl-ppstructure.json"),
                    }
                )
            except Exception as exc:
                msg = f"PPStructureV3 predict 失败: {type(exc).__name__}: {exc}"
                print(f"  {msg}")
                errors.append(msg)
                results.append({"method": "PPStructureV3", "status": "error", "error": str(exc)[:300]})
        else:
            errors.append("PPStructureV3 初始化失败（环境不可用）")
            results.append({"method": "PPStructureV3", "status": "skipped", "reason": "环境不可用"})

    # --- TableRecognitionPipelineV2 ---
    if use_table_pipeline:
        print("=" * 60)
        print("[Paddle VL] 测试 TableRecognitionPipelineV2 ...")
        t0 = time.perf_counter()
        engine = _init_paddlevl_table_pipeline()
        if engine is not None:
            try:
                raw = engine.predict(str(pdf_path))
                elapsed = time.perf_counter() - t0
                info = _extract_paddle_result(raw, "TableRecognitionPipelineV2")
                info["elapsed_seconds"] = round(elapsed, 2)
                write_json(output_dir / "paddlevl-table-pipeline.json", info)

                for i, tbl in enumerate(info["table_items"]):
                    if tbl.get("table_html"):
                        write_text(
                            output_dir / f"paddlevl-table-pipeline-table-{i + 1}.html",
                            f"<!DOCTYPE html><html><head><meta charset='utf-8'>"
                            f"<style>table{{border-collapse:collapse;width:100%}}"
                            f"td,th{{border:1px solid #ccc;padding:6px;text-align:left}}"
                            f"</style></head><body>{tbl['table_html']}</body></html>",
                        )

                print(
                    f"  TableRecognitionPipelineV2 完成: {elapsed:.1f}s, "
                    f"表格={info['table_count']}"
                )
                results.append(
                    {
                        "method": "TableRecognitionPipelineV2",
                        "status": "ok",
                        "elapsed_seconds": round(elapsed, 2),
                        "table_count": info["table_count"],
                        "output_file": str(output_dir / "paddlevl-table-pipeline.json"),
                    }
                )
            except Exception as exc:
                msg = f"TableRecognitionPipelineV2 predict 失败: {type(exc).__name__}: {exc}"
                print(f"  {msg}")
                errors.append(msg)
                results.append(
                    {"method": "TableRecognitionPipelineV2", "status": "error", "error": str(exc)[:300]}
                )
        else:
            errors.append("TableRecognitionPipelineV2 初始化失败（环境不可用）")
            results.append(
                {"method": "TableRecognitionPipelineV2", "status": "skipped", "reason": "环境不可用"}
            )

    # --- PaddleOCRVL ---
    if use_ocrvl:
        print("=" * 60)
        print("[Paddle VL] 测试 PaddleOCRVL (VLM 文档理解) ...")
        t0 = time.perf_counter()
        engine = _init_paddlevl_ocrvl()
        if engine is not None:
            try:
                raw = engine.predict(str(pdf_path))
                elapsed = time.perf_counter() - t0
                info = _extract_paddle_result(raw, "PaddleOCRVL")
                info["elapsed_seconds"] = round(elapsed, 2)
                write_json(output_dir / "paddlevl-ocrvl.json", info)
                write_text(output_dir / "paddlevl-ocrvl.md", info["markdown"])

                print(f"  PaddleOCRVL 完成: {elapsed:.1f}s")
                results.append(
                    {
                        "method": "PaddleOCRVL",
                        "status": "ok",
                        "elapsed_seconds": round(elapsed, 2),
                        "table_count": info["table_count"],
                        "output_file": str(output_dir / "paddlevl-ocrvl.json"),
                    }
                )
            except Exception as exc:
                msg = f"PaddleOCRVL predict 失败: {type(exc).__name__}: {exc}"
                print(f"  {msg}")
                errors.append(msg)
                results.append({"method": "PaddleOCRVL", "status": "error", "error": str(exc)[:300]})
        else:
            errors.append("PaddleOCRVL 初始化失败（环境不可用）")
            results.append({"method": "PaddleOCRVL", "status": "skipped", "reason": "环境不可用"})

    return {
        "success": any(r.get("status") == "ok" for r in results),
        "results": results,
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# MinerU 表格解析
# ---------------------------------------------------------------------------


def test_mineru(pdf_path: Path, output_dir: Path) -> dict:
    """使用 MinerU 解析表格 PDF。

    返回 {"success": bool, "error": str|None, "summary": {...}}
    """
    print("=" * 60)
    print("[MinerU] 测试 MinerU 表格解析 ...")

    settings = Settings(
        data_dir=output_dir / "runtime",
        scan_parser="mineru",
        max_pdf_pages=20,
    )
    mineru = MinerUParser(settings)
    command = mineru.command()
    if not command:
        print("  MinerU 命令未找到，跳过")
        return {
            "success": False,
            "error": "MinerU CLI 命令未找到",
            "command": None,
            "summary": None,
        }

    print(f"  MinerU 命令: {command}")

    def progress(value: float, message: str) -> None:
        print(f"  [MinerU] {value:5.1f}% {message}")

    t0 = time.perf_counter()
    try:
        mineru_out = output_dir / "mineru-output"
        parsed = mineru.parse(pdf_path, mineru_out, progress)
        elapsed = time.perf_counter() - t0

        # 保存解析结果
        write_text(output_dir / "mineru.md", parsed.markdown)

        # 提取表格块和文字块
        table_blocks: list[dict] = []
        text_blocks: list[dict] = []
        image_blocks: list[dict] = []
        other_blocks: list[dict] = []

        for block in parsed.blocks:
            block_info = {
                "page": block.page,
                "text_preview": block.text[:300] if block.text else "",
                "text_length": len(block.text) if block.text else 0,
                "type": block.block_type,
                "bbox": block.bbox,
                "level": block.level,
                "printed_page": block.printed_page,
            }
            if "table" in block.block_type.lower():
                table_blocks.append(block_info)
            elif block.block_type in ("text", "paragraph", "title"):
                text_blocks.append(block_info)
            elif block.block_type in ("image", "img", "figure"):
                image_blocks.append(block_info)
            else:
                other_blocks.append(block_info)

        normalized = {
            "method": "MinerU",
            "command": command,
            "pages": parsed.pages,
            "parser_name": parsed.parser_name,
            "needs_ocr": parsed.needs_ocr,
            "total_blocks": len(parsed.blocks),
            "table_blocks": table_blocks,
            "table_count": len(table_blocks),
            "text_blocks": text_blocks,
            "text_block_count": len(text_blocks),
            "image_blocks": image_blocks,
            "image_count": len(image_blocks),
            "other_blocks": other_blocks,
            "markdown_length": len(parsed.markdown),
            "elapsed_seconds": round(elapsed, 2),
        }
        write_json(output_dir / "mineru-normalized.json", normalized)

        # 切分测试
        chunks = build_chunks("table-test", parsed.blocks)
        write_json(
            output_dir / "mineru-chunks.json",
            [
                {
                    "ordinal": c.ordinal,
                    "chapter_path": c.chapter_path,
                    "clause_no": c.clause_no,
                    "text": c.text,
                    "source_type": c.source_type,
                    "page_start": c.page_start,
                    "page_end": c.page_end,
                }
                for c in chunks
            ],
        )

        print(
            f"  MinerU 完成: {elapsed:.1f}s, "
            f"blocks={len(parsed.blocks)}, "
            f"表格={len(table_blocks)}, "
            f"文字={len(text_blocks)}, "
            f"chunks={len(chunks)}"
        )

        return {
            "success": True,
            "error": None,
            "command": command,
            "elapsed_seconds": round(elapsed, 2),
            "summary": {
                "pages": parsed.pages,
                "total_blocks": len(parsed.blocks),
                "table_count": len(table_blocks),
                "table_html_count": sum(
                    1 for b in table_blocks if "<table" in (b.get("text_preview") or "")
                ),
                "text_block_count": len(text_blocks),
                "image_count": len(image_blocks),
                "markdown_length": len(parsed.markdown),
                "chunk_count": len(chunks),
            },
        }

    except Exception as exc:
        elapsed = time.perf_counter() - t0
        print(f"  MinerU 失败 ({elapsed:.1f}s): {type(exc).__name__}: {exc}")
        return {
            "success": False,
            "error": f"{type(exc).__name__}: {exc}",
            "elapsed_seconds": round(elapsed, 2),
            "command": command,
            "summary": None,
        }


# ---------------------------------------------------------------------------
# PDF 基本信息
# ---------------------------------------------------------------------------


def inspect_pdf_basic(pdf_path: Path) -> dict:
    """获取 PDF 基本信息用于报告。"""
    with fitz.open(pdf_path) as doc:
        info: dict = {
            "file": str(pdf_path),
            "file_size_bytes": pdf_path.stat().st_size,
            "pages": doc.page_count,
        }
        if doc.page_count > 0:
            page = doc[0]
            text = page.get_text("text")
            info["page1_text_length"] = len(text.strip())
            info["page1_text_preview"] = text.strip()[:500]
            # PyMuPDF 表格检测
            if hasattr(page, "find_tables"):
                try:
                    tables = page.find_tables()
                    info["pymupdf_table_count"] = len(tables.tables)
                except Exception:
                    info["pymupdf_table_count"] = -1
        return info


# ---------------------------------------------------------------------------
# 对比汇总
# ---------------------------------------------------------------------------


def build_comparison_report(
    pdf_info: dict,
    paddle_result: dict,
    mineru_result: dict,
    output_dir: Path,
) -> dict:
    """生成对比汇总报告。"""
    report: dict = {
        "pdf": pdf_info,
        "paddle_vl": paddle_result,
        "mineru": {
            "success": mineru_result.get("success"),
            "error": mineru_result.get("error"),
            "command": mineru_result.get("command"),
            "elapsed_seconds": mineru_result.get("elapsed_seconds"),
            "summary": mineru_result.get("summary"),
        },
        "comparison": {},
    }

    # 提取成功的方法用于对比
    paddle_methods = [
        r for r in paddle_result.get("results", []) if r.get("status") == "ok"
    ]
    mineru_ok = mineru_result.get("success")

    # 时间对比
    timings: dict = {}
    for m in paddle_methods:
        if m.get("elapsed_seconds"):
            timings[m["method"]] = m["elapsed_seconds"]
    if mineru_ok and mineru_result.get("elapsed_seconds"):
        timings["MinerU"] = mineru_result["elapsed_seconds"]
    report["comparison"]["timing_seconds"] = timings

    # 表格数量对比
    table_counts: dict = {}
    for m in paddle_methods:
        table_counts[m["method"]] = m.get("table_count", 0)
    if mineru_ok and mineru_result.get("summary"):
        table_counts["MinerU"] = mineru_result["summary"].get("table_count", 0)
    report["comparison"]["table_counts"] = table_counts

    # 输出文件列表
    output_files = [str(p.relative_to(output_dir)) for p in sorted(output_dir.rglob("*")) if p.is_file()]
    report["output_files"] = output_files

    return report


# ---------------------------------------------------------------------------
# 主函数
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="表格 PDF 解析对比测试：Paddle VL vs MinerU",
    )
    parser.add_argument(
        "--pdf",
        default="tests/test/table3.pdf",
        help="输入 PDF 路径 (默认: tests/test/table3.pdf)",
    )
    parser.add_argument(
        "--out",
        default="tests/test/table3_compare",
        help="输出目录 (默认: test/table3_compare)",
    )
    parser.add_argument(
        "--skip-paddle",
        action="store_true",
        help="跳过 Paddle VL 测试",
    )
    parser.add_argument(
        "--skip-mineru",
        action="store_true",
        help="跳过 MinerU 测试",
    )
    parser.add_argument(
        "--paddle-ocrvl",
        action="store_true",
        help="额外使用 PaddleOCRVL (VLM 文档理解) 测试",
    )
    parser.add_argument(
        "--ppstructure-only",
        action="store_true",
        help="只使用 PPStructureV3（跳过 TableRecognitionPipelineV2）",
    )
    args = parser.parse_args()

    pdf_path = Path(args.pdf).resolve()
    output_dir = Path(args.out).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    if not pdf_path.exists():
        print(f"错误: PDF 文件不存在: {pdf_path}")
        sys.exit(1)

    print("=" * 60)
    print(f"输入文件: {pdf_path}")
    print(f"文件大小: {pdf_path.stat().st_size:,} bytes")
    print(f"输出目录: {output_dir}")
    print()

    # Step 1: PDF 基本信息
    print("=" * 60)
    print("[INFO] 获取 PDF 基本信息 ...")
    pdf_info = inspect_pdf_basic(pdf_path)
    write_json(output_dir / "pdf-info.json", pdf_info)
    print(f"  页数: {pdf_info['pages']}")
    print(f"  第1页文本长度: {pdf_info.get('page1_text_length', 'N/A')}")
    print(f"  PyMuPDF 表格数: {pdf_info.get('pymupdf_table_count', 'N/A')}")

    # Step 2: 渲染首页
    print()
    img_path = render_first_page(pdf_path, output_dir)
    print(f"[INFO] 首页截图: {img_path}")

    # Step 3: Paddle VL 解析
    print()
    paddle_result: dict = {"success": False, "results": [], "errors": []}
    if not args.skip_paddle:
        paddle_result = test_paddle_vl(
            pdf_path,
            output_dir,
            use_ppstructure=True,
            use_table_pipeline=not args.ppstructure_only,
            use_ocrvl=args.paddle_ocrvl,
        )
    else:
        print("[SKIP] 跳过 Paddle VL 测试")

    # Step 4: MinerU 解析
    print()
    mineru_result: dict = {"success": False, "error": "skipped", "command": None, "summary": None}
    if not args.skip_mineru:
        mineru_result = test_mineru(pdf_path, output_dir)
    else:
        print("[SKIP] 跳过 MinerU 测试")

    # Step 5: 对比汇总
    print()
    print("=" * 60)
    print("[SUMMARY] 生成对比报告 ...")
    report = build_comparison_report(pdf_info, paddle_result, mineru_result, output_dir)
    write_json(output_dir / "comparison-report.json", report)

    # 打印摘要
    print()
    print("=" * 60)
    print("                    测 试 结 果 摘 要")
    print("=" * 60)

    # Paddle 结果
    print("\n--- Paddle VL ---")
    if paddle_result["errors"]:
        for err in paddle_result["errors"]:
            print(f"  [WARN] {err[:120]}")
    for r in paddle_result["results"]:
        status_icon = "[OK]" if r["status"] == "ok" else "[SKIP]" if r["status"] == "skipped" else "[ERR]"
        if r["status"] == "ok":
            print(
                f"  {status_icon} {r['method']}: "
                f"耗时={r.get('elapsed_seconds', '?')}s, "
                f"表格={r.get('table_count', '?')}个, "
                f"文字块={r.get('text_block_count', '?')}个"
            )
        else:
            print(f"  {status_icon} {r['method']}: {r.get('reason', r.get('error', '?'))}")

    # MinerU 结果
    print("\n--- MinerU ---")
    if mineru_result["success"]:
        s = mineru_result["summary"]
        print(
            f"  [OK] MinerU: "
            f"耗时={mineru_result['elapsed_seconds']}s, "
            f"blocks={s['total_blocks']}, "
            f"表格={s['table_count']}个 (含HTML={s.get('table_html_count', 0)}), "
            f"chunks={s['chunk_count']}"
        )
    else:
        print(f"  [SKIP/ERR] MinerU: {mineru_result.get('error', '未执行')}")

    # 对比
    print("\n--- 对比 ---")
    for k, v in report["comparison"].items():
        if isinstance(v, dict):
            print(f"  {k}:")
            for mk, mv in v.items():
                print(f"    {mk}: {mv}")
        else:
            print(f"  {k}: {v}")

    print(f"\n所有输出文件: {output_dir}")
    print(f"对比报告: {output_dir / 'comparison-report.json'}")


if __name__ == "__main__":
    main()
