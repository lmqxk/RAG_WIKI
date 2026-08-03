from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
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


def main() -> int:
    missing = [path for path in (PYTHON, NODE, NPM_CLI) if not path.exists()]
    if missing:
        print("缺少项目运行环境，请先按 README 执行初始化：")
        for path in missing:
            print(f"  - {path}")
        return 2

    environment = os.environ.copy()
    environment.update(
        {
            "UV_CACHE_DIR": str(ROOT / ".tools" / "uv-cache"),
            "npm_config_cache": str(ROOT / ".tools" / "npm-cache"),
        }
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
    processes = [api, web]

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
        api_ready = wait_for("http://127.0.0.1:8000/api/health")
        web_ready = wait_for("http://127.0.0.1:3000/")
        if api_ready and web_ready:
            print("\n规智库已启动：")
            print("  Web: http://localhost:3000")
            print("  API: http://127.0.0.1:8000/docs")
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
