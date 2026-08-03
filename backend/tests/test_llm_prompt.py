r"""手动测试一次 LLM 提示词是否会正确归纳表格证据。

用法：
  backend\.venv\Scripts\python.exe scripts\test_llm_prompt.py
  backend\.venv\Scripts\python.exe scripts\test_llm_prompt.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))

from backend.config import Settings  # noqa: E402
from backend.domain import SearchHit  # noqa: E402
from backend.providers import ChatProvider, clean_answer_text  # noqa: E402


QUESTION = "石膏空心条板隔墙燃烧性能"
QUERY_TYPE = "fact"


def make_hits() -> list[SearchHit]:
    return [
        SearchHit(
            chunk_id="test-1",
            document_id="table2",
            text=(
                "表格行：序号：8；构件名称：石膏空心条板隔墙；"
                "构件名称：1. 石膏珍珠岩空心条板，膨胀珍珠岩的容重为(50~80)kg/m³；"
                "构件厚度或截面最小尺寸(mm)：60；耐火极限(h)：1.50；燃烧性能：不燃性"
            ),
            clause_no=None,
            chapter_path="",
            page_start=2,
            page_end=2,
            printed_page=None,
            document_title="table2",
            standard_no=None,
            version=None,
            score=1.0,
            source="manual",
            content_type="normative_table",
        ),
        SearchHit(
            chunk_id="test-2",
            document_id="table2",
            text=(
                "表格行：序号：8；构件名称：石膏空心条板隔墙；"
                "构件名称：2. 石膏珍珠岩空心条板，膨胀珍珠岩的容重为(60~120)kg/m³；"
                "构件厚度或截面最小尺寸(mm)：60；耐火极限(h)：1.20；燃烧性能：不燃性"
            ),
            clause_no=None,
            chapter_path="",
            page_start=2,
            page_end=2,
            printed_page=None,
            document_title="table2",
            standard_no=None,
            version=None,
            score=0.98,
            source="manual",
            content_type="normative_table",
        ),
        SearchHit(
            chunk_id="test-3",
            document_id="table2",
            text=(
                "表格行：序号：8；构件名称：石膏空心条板隔墙；"
                "构件名称：3. 石膏珍珠岩塑料网空心条板，膨胀珍珠岩的容重为（60～120)kg/m³；"
                "构件厚度或截面最小尺寸(mm)：60；耐火极限(h)：1.30；燃烧性能：不燃性"
            ),
            clause_no=None,
            chapter_path="",
            page_start=2,
            page_end=2,
            printed_page=None,
            document_title="table2",
            standard_no=None,
            version=None,
            score=0.96,
            source="manual",
            content_type="normative_table",
        ),
    ]


def evidence_json(hits: list[SearchHit]) -> str:
    return json.dumps(
        [
            {
                "id": index,
                "document": hit.document_title,
                "standard_no": hit.standard_no,
                "version": hit.version,
                "location": {
                    "chapter_path": hit.chapter_path or None,
                    "clause_no": hit.clause_no,
                    "pdf_page": hit.page_start,
                },
                "type": "规范正文" if hit.content_type.startswith("normative") else "条文说明",
                "text": hit.text,
            }
            for index, hit in enumerate(hits, 1)
        ],
        ensure_ascii=False,
        indent=2,
    )


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="只打印测试输入，不调用 LLM")
    args = parser.parse_args()

    settings = Settings()
    provider = ChatProvider(settings)
    hits = make_hits()

    print("=== LLM 配置 ===")
    print(f"llm_configured: {provider.external}")
    print(f"base_url: {settings.openai_base_url}")
    print(f"chat_model: {settings.chat_model}")
    print()

    print("=== 问题 ===")
    print(QUESTION)
    print()

    print("=== 证据 JSON ===")
    print(evidence_json(hits))
    print()

    if args.dry_run:
        print("dry-run: 已跳过真实 LLM 调用。")
        return 0

    if not provider.external:
        print("未加载外部 LLM：请检查 RAG_OPENAI_API_KEY 和 RAG_CHAT_MODEL。")
        return 2

    print("=== 原始回答 ===")
    raw = provider._external_answer(QUESTION, hits, QUERY_TYPE)
    print(raw)
    print()

    print("=== 清洗后回答 ===")
    print(clean_answer_text(raw))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
