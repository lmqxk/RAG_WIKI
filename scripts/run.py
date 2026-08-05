from __future__ import annotations

import os
import json
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
BACKEND_SRC = BACKEND / "src"
FRONTEND = ROOT / "frontend"
PYTHON = BACKEND / ".venv" / "Scripts" / "python.exe"
NODE = (
    Path(r"C:\Users\PC\.cache\codex-runtimes\codex-primary-runtime")
    / "dependencies"
    / "node"
    / "bin"
    / "node.exe"
)
NPM_CLI = ROOT / ".tools" / "npm-cli" / "package" / "bin" / "npm-cli.js"

sys.path.insert(0, str(BACKEND_SRC))
from backend.config import get_settings  # noqa: E402


def local_rerank_enabled(url: str | None) -> bool:
    if not url:
        return False
    parsed = urlparse(url)
    return parsed.hostname in {"127.0.0.1", "localhost"} and parsed.path.rstrip("/") == "/rerank"


def wait_for(url: str, timeout: float = 30) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                if response.status < 500:
                    return True
        except OSError:
            time.sleep(0.4)
    return False


def warmup_rerank(settings) -> bool:
    if not settings.local_rerank_warmup_on_start:
        return True
    url = f"http://{settings.local_rerank_host}:{settings.local_rerank_port}/rerank"
    request = urllib.request.Request(
        url,
        data=json.dumps(
            {
                "model": settings.rerank_model or "jina-reranker-v3.5",
                "query": "系统启动预热",
                "documents": ["系统启动预热资料"],
                "top_n": 1,
            }
        ).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer local"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return 200 <= response.status < 300
    except OSError as exc:
        print(f"Rerank 预热失败，将在首次请求时重试：{exc}")
        return False


def main() -> int:
    missing = [path for path in (PYTHON, NODE, NPM_CLI) if not path.exists()]
    if missing:
        print("缺少项目运行环境，请先按 README 执行初始化：")
        for path in missing:
            print(f"  - {path}")
        return 2

    settings = get_settings()
    environment = os.environ.copy()
    environment.update(
        {
            "UV_CACHE_DIR": str(ROOT / ".tools" / "uv-cache"),
            "npm_config_cache": str(ROOT / ".tools" / "npm-cache"),
        }
    )
    rerank = None
    if local_rerank_enabled(settings.rerank_base_url):
        rerank = subprocess.Popen(
            [
                str(PYTHON),
                "-m",
                "uvicorn",
                "backend.rerank_server:app",
                "--host",
                settings.local_rerank_host,
                "--port",
                str(settings.local_rerank_port),
            ],
            cwd=BACKEND,
            env=environment,
        )
    api = subprocess.Popen(
        [
            str(PYTHON),
            "-m",
            "uvicorn",
            "backend.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            "8000",
        ],
        cwd=BACKEND,
        env=environment,
    )
    web = subprocess.Popen(
        [str(NODE), str(NPM_CLI), "run", "dev"],
        cwd=FRONTEND,
        env={**environment, "PATH": f"{NODE.parent};{environment.get('PATH', '')}"},
    )
    processes = [process for process in (rerank, api, web) if process is not None]

    def stop(_signum: int | None = None, _frame: object | None = None) -> None:
        for process in processes:
            if process.poll() is None:
                process.terminate()
        for process in processes:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    try:
        rerank_ready = True
        if rerank is not None:
            rerank_ready = wait_for(
                f"http://{settings.local_rerank_host}:{settings.local_rerank_port}/health",
                timeout=20,
            )
            if rerank_ready:
                warmup_rerank(settings)
        api_ready = wait_for("http://127.0.0.1:8000/api/health")
        web_ready = wait_for("http://127.0.0.1:3000/")
        if api_ready and web_ready:
            print("\n规智库已启动：")
            print("  Web: http://localhost:3000")
            print("  API: http://127.0.0.1:8000/docs")
            if rerank is not None:
                status = "已启动" if rerank_ready else "启动中/不可用，主后端会降级本地排序"
                print(
                    "  Rerank: "
                    f"http://{settings.local_rerank_host}:{settings.local_rerank_port}/rerank"
                    f" ({status})"
                )
            print("按 Ctrl+C 停止服务。\n")
        else:
            print("服务启动超时，请查看上方日志。")
        while all(process.poll() is None for process in processes):
            time.sleep(0.5)
    finally:
        stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
