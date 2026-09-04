"""测试火山方舟 CodePlan / OpenAI 兼容 Chat API 是否可用。"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import dotenv
import httpx


PROJECT_ROOT = Path(__file__).resolve().parent
dotenv.load_dotenv(PROJECT_ROOT / ".env")

BASE_URL = os.getenv(
    "RAG_OPENAI_BASE_URL",
    "https://ark.cn-beijing.volces.com/api/v3",
).rstrip("/")
API_KEY = os.getenv("RAG_OPENAI_API_KEY") or os.getenv("ARK_API_KEY")
MODEL = os.getenv("RAG_CHAT_MODEL") or os.getenv("ARK_MODEL")


def main() -> int:
    if not API_KEY:
        print("缺少 API Key，请设置 RAG_OPENAI_API_KEY 或 ARK_API_KEY")
        return 2
    if not MODEL:
        print("缺少模型名称，请设置 RAG_CHAT_MODEL 或 ARK_MODEL")
        return 2

    url = f"{BASE_URL}/chat/completions"
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": "你是 API 连通性测试助手。"},
            {"role": "user", "content": "只回复：CodePlan API 正常"},
        ],
        "temperature": 0,
        "max_tokens": 32,
    }

    print(f"请求地址: {url}")
    print(f"模型: {MODEL}")
    try:
        response = httpx.post(
            url,
            headers={"Authorization": f"Bearer {API_KEY}"},
            json=payload,
            timeout=60,
        )
    except httpx.HTTPError as exc:
        print(f"请求失败: {exc}")
        return 1

    print(f"HTTP 状态码: {response.status_code}")
    try:
        data = response.json()
    except json.JSONDecodeError:
        print(response.text[:1000])
        return 1

    if response.is_error:
        print(json.dumps(data, ensure_ascii=False, indent=2)[:2000])
        return 1

    choices = data.get("choices") or []
    content = choices[0].get("message", {}).get("content") if choices else None
    print(f"模型响应: {content or '(没有返回文本)'}")
    print("CodePlan API 调用成功")
    return 0


if __name__ == "__main__":
    sys.exit(main())
