# 10 - 环境配置与恢复

## 当前环境来源

本项目后端当前使用项目内虚拟环境：

```text
backend/.venv
```

该环境不是系统 Python，也不是全局 pip 环境。`backend/.venv/pyvenv.cfg` 显示：

```text
home = C:\Users\PC\.cache\codex-runtimes\codex-primary-runtime\dependencies\python
implementation = CPython
uv = 0.12.0
version_info = 3.12.13
include-system-site-packages = false
```

因此可以判断：这个 `.venv` 是由 `uv` 创建/同步出来的虚拟环境，Python 来自 Codex runtime 缓存目录。

## 为什么识别不到 uv 或 pip

当前 PowerShell 里：

```powershell
uv
pip
python
```

可能都识别不到，原因是这些命令不在系统 `PATH` 中。

另外，当前虚拟环境里没有 pip 模块：

```powershell
.\backend\.venv\Scripts\python.exe -m pip --version
```

会返回：

```text
No module named pip
```

这属于 `uv` 管理环境的常见形态：项目运行依赖已经装进 `.venv`，但不依赖虚拟环境里的 pip。

## 运行项目

优先使用项目固定入口：

```powershell
cd E:\lmq\RAG_ZB
.\start.cmd
```

`start.cmd` 会调用：

```text
backend\.venv\Scripts\python.exe scripts\run.py
```

`scripts/run.py` 会启动：

```text
后端: http://127.0.0.1:8000
前端: http://localhost:3000
本地重排: http://127.0.0.1:8011/rerank（仅当 RAG_RERANK_BASE_URL 指向本地 /rerank 时）
```

本地重排服务是独立进程。它会在第一次收到 `/rerank` 请求时懒加载 `jina-reranker-v3.5`，优先使用 GPU，GPU 加载或推理失败时降级 CPU。即使本地重排不可用，主后端也会回退到本地词项排序，不会阻断问答。

它使用的 Node 和 npm-cli 也不是系统全局环境，而是项目/运行时内置路径：

```text
C:\Users\PC\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe
E:\lmq\RAG_ZB\.tools\npm-cli\package\bin\npm-cli.js
```

## 后端常用命令

不依赖系统 `python`，直接用项目虚拟环境：

```powershell
cd E:\lmq\RAG_ZB\backend
.\.venv\Scripts\python.exe -m pytest --basetemp .pytest-tmp -o cache_dir=.pytest-cache-run
```

启动后端：

```powershell
cd E:\lmq\RAG_ZB\backend
.\.venv\Scripts\python.exe -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

查看 Python 版本：

```powershell
cd E:\lmq\RAG_ZB
.\backend\.venv\Scripts\python.exe --version
```

## 依赖声明位置

后端依赖声明在：

```text
backend/pyproject.toml
backend/uv.lock
```

核心依赖：

```text
fastapi
httpx
numpy
pydantic-settings
pymupdf
pypdf
python-multipart
qdrant-client
uvicorn
```

可选依赖组：

```text
mineru
ocr
dev
```

## 恢复或重建后端环境

如果本机能使用 `uv`，推荐在 `backend` 目录执行：

```powershell
cd E:\lmq\RAG_ZB\backend
uv sync --all-extras --group dev
```

如果只需要基础运行环境：

```powershell
cd E:\lmq\RAG_ZB\backend
uv sync
```

如果需要 MinerU 和 OCR：

```powershell
cd E:\lmq\RAG_ZB\backend
uv sync --extra mineru --extra ocr --group dev
```

注意：当前系统 PATH 里没有 `uv`，所以以上命令只有在安装或暴露 `uv` 后才能执行。

## 不建议的做法

不要直接用系统 pip 往项目里混装依赖：

```powershell
pip install ...
```

原因：

- 当前 `.venv` 没有 pip。
- 系统 pip 可能写到别的 Python 环境。
- 项目已有 `uv.lock`，混装容易造成依赖版本和锁文件不一致。

也不要手动编辑 `.env`、密钥、token 或 API 配置；这类修改需要先确认。

## 当前环境排查命令

查看虚拟环境来源：

```powershell
Get-Content E:\lmq\RAG_ZB\backend\.venv\pyvenv.cfg
```

查看虚拟环境可执行文件：

```powershell
Get-ChildItem E:\lmq\RAG_ZB\backend\.venv\Scripts | Select-Object Name
```

查看已安装入口：

```powershell
Get-ChildItem E:\lmq\RAG_ZB\backend\.venv\Scripts | Select-String "uvicorn|pytest|rag"
```

查看后端服务健康状态：

```powershell
Invoke-WebRequest -Uri "http://127.0.0.1:8000/api/health"
```
