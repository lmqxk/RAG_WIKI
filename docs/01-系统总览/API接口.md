# API 接口

主要文件：

- `backend/src/backend/main.py`
- `backend/src/backend/schemas.py`
- `backend/src/backend/config.py`

## 模块定位

API 入口负责创建应用、装配依赖、暴露 HTTP 接口，并在应用生命周期内管理后台入库线程和 Qdrant 客户端连接。

## 组件装配

`build_components()` 按顺序创建以下对象：

| 组件 | 职责 |
| --- | --- |
| `Database` | SQLite 连接与表初始化 |
| `Repository` | 文档、任务、文本块持久化 |
| `DocumentParser` | PDF 解析 |
| `EmbeddingProvider` | 文本向量生成（Qwen3-Embedding via vLLM） |
| `VectorIndex` | Qdrant 集合管理 |
| `RerankProvider` | 检索结果排序（Qwen3-Reranker via vLLM） |
| `HybridRetriever` | 检索编排 |
| `ChatProvider` | 回答生成 |
| `IngestionManager` | 后台入库任务 |
| `RagService` | 上传和问答业务服务 |
| `WikiManager` | Wiki 派生层同步 |

## FastAPI 技术细节

- 应用对象：`app = FastAPI(...)`
- 生命周期：`@asynccontextmanager lifespan`
- CORS：`CORSMiddleware`
- 文件上传：`UploadFile` + `File()`
- PDF 文件响应：`FileResponse`
- 请求响应模型：Pydantic `BaseModel`
- 启动方式：`uvicorn.run("backend.main:app", ...)`

`app.state` 保存长期依赖对象，避免每次请求重复初始化 SQLite、Qdrant 和 Provider。

## 生命周期

FastAPI `lifespan` 启动时会：

- 配置文件日志（`storage/logs/backend.log`，RotatingFileHandler 滚动 10MB×5）
- 读取 `Settings`
- 初始化 SQLite 表结构
- 初始化 Qdrant 集合
- 恢复未完成的入库任务

应用关闭时会：

- 停止入库线程池继续接新任务
- 关闭 Qdrant client

## 路由清单

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/health` | 服务、解析器、模型配置状态 |
| `GET` | `/api/documents` | 获取文档列表 |
| `POST` | `/api/documents` | 上传 PDF 并创建入库任务 |
| `PATCH` | `/api/documents/{document_id}` | 修改文档标题、编号、版本等元数据 |
| `POST` | `/api/documents/{document_id}/reparse` | 替换已上传 PDF，并从头重新解析、切分和索引 |
| `GET` | `/api/jobs/{job_id}` | 查询解析或重建索引任务 |
| `GET` | `/api/documents/{document_id}/file` | 返回 PDF 原文件 |
| `GET` | `/api/documents/{document_id}/asset` | 返回解析产物中的图片等静态资源 |
| `POST` | `/api/documents/{document_id}/reindex` | 复用解析结果重建索引 |
| `GET` | `/api/wiki/concepts` | Wiki 概念列表 |
| `GET` | `/api/wiki/concepts/{name}` | 概念详情 |
| `GET` | `/api/wiki/graph` | 概念关系图数据 |
| `GET` | `/api/wiki/pages/{page_id:path}` | Wiki 页面内容 |
| `POST` | `/api/chat` | 提问并返回回答与引用 |
| `POST` | `/api/chat/stream` | 流式问答（前端默认入口） |

## 静态资源路径解析

`resolve_storage_path()` 负责把数据库里记录的存储路径映射回宿主机真实文件路径：

- 优先使用原始记录路径
- 回退到 `data_dir/subdir/文件名`
- 额外检查子目录层级（`storage/parsed/<document_id>/...` 的多级结构），保证 Wiki 锚点、引用图片不出现 404

## 错误处理

- 上传校验失败返回 `400`。
- 文档或任务不存在返回 `404`。
- 问答处理异常返回 `500`，错误信息放在 `detail` 中。

## 运维思考

| 维度 | 考量 |
| --- | --- |
| 高并发 | Uvicorn 单进程异步 IO；上传解析走 `ThreadPoolExecutor`（默认 1 worker），避免大 PDF 并发解析抢占内存；问答请求本身无全局锁 |
| 时延 | `/api/chat` 为长请求（60s 量级），前端必须用 `/api/chat/stream` 流式接口避免超时；非流式接口保留用于脚本调试 |
| 端口 | Docker 部署固定 `8008`（`RAG_API_PORT`），本地开发默认 `8000`；两者共用同一 `storage/`，不可同时运行 |
| 可观测性 | uvicorn 与 uvicorn.access 双 logger 落盘 `storage/logs/backend.log`；问答指标写 `chat-metrics.jsonl` |
| 安全 | 模型服务端口（8010/8011）只绑定 `127.0.0.1`；`.env` 不入库（gitignore），密钥不进代码 |

## 维护注意

- 新增接口时先在 `schemas.py` 定义请求和响应模型。
- 不要在路由层塞复杂业务逻辑，优先放到 `service.py` 或专用模块。
- 修改 CORS、端口、模型地址等配置属于 API 配置变更，需要先确认。

## API 调试命令

```powershell
curl http://127.0.0.1:8008/api/health
curl http://127.0.0.1:8008/api/documents
```

问答接口示例：

```powershell
curl -X POST http://127.0.0.1:8008/api/chat `
  -H "Content-Type: application/json" `
  -d "{\"question\":\"建筑防火通用规范 4.1.4 条规定了什么？\"}"
```
