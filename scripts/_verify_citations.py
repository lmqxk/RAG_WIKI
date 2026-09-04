"""临时验证：引用编号对齐与动态数量。"""

from __future__ import annotations

import json
import re
import sys
import urllib.request

BASE = "http://127.0.0.1:8008"


def stream(question: str) -> None:
    request = urllib.request.Request(
        f"{BASE}/api/chat/stream",
        data=json.dumps({"question": question}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    meta = None
    answer: list[str] = []
    with urllib.request.urlopen(request, timeout=180) as response:
        for raw in response:
            line = raw.decode("utf-8").strip()
            if not line:
                continue
            event = json.loads(line)
            if event.get("event") == "meta":
                meta = event
            elif event.get("event") == "delta":
                answer.append(event.get("text", ""))
    assert meta is not None
    text = "".join(answer)
    cited = sorted({int(m) for m in re.findall(r"\[(\d{1,2})\]", text)})
    print(f"问题: {question}")
    print(f"citations={meta.get('citation_count')} | 回答中出现的编号: {cited}")
    print(f"答案开头: {text[:180]}")
    # 验证引用的 PDF 链接可访问
    docs = json.load(urllib.request.urlopen(f"{BASE}/api/documents", timeout=10))
    ok = 0
    for doc in docs[:3]:
        with urllib.request.urlopen(
            f"{BASE}/api/documents/{doc['id']}/file", timeout=10
        ) as r:
            if r.status == 200:
                ok += 1
    print(f"PDF 接口可达: {ok}/3")


def main() -> int:
    stream("甲类厂房与明火地点的防火间距是多少？")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
