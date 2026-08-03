# 03 - API 入口

## 模块定位

API 入口负责创建应用、装配依赖、暴露 HTTP 接口，并在应用生命周期内管理后台入库线程和本地索引连接。

主要文件：

- `backend/src/backend/main.py`
- `backend/src/backend/schemas.py`
- `backend/src/backend/config.py`

## 组件装配

`build_components()` 会按顺序创建以下对象：

| 组件 | 职责 |
| --- | --- |
| `Database` | SQLite 连接与表初始化 |
| `Repository` | 文档、任务、文本块持久化 |
| `DocumentParser` | PDF 解析 |
| `EmbeddingProvider` | 文本向量生成 |
| `VectorIndex` | Qdrant 本地集合管理 |
| `RerankProvider` | 检索结果排序 |
| `HybridRetriever` | 检索编排 |
| `ChatProvider` | 回答生成 |
| `IngestionManager` | 后台入库任务 |
| `RagService` | 上传和问答业务服务 |

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
| `POST` | `/api/documents/{document_id}/reindex` | 复用解析结果重建索引 |
| `POST` | `/api/chat` | 提问并返回回答与引用 |

## 错误处理

- 上传校验失败返回 `400`。
- 文档或任务不存在返回 `404`。
- 问答处理异常返回 `500`，错误信息会放在 `detail` 中。

## 维护注意

- 新增接口时先在 `schemas.py` 定义请求和响应模型。
- 不要在路由层塞复杂业务逻辑，优先放到 `service.py` 或专用模块。
- 修改 CORS、端口、模型地址等配置属于 API 配置变更，需要先确认。

## API 调试命令

```powershell
curl http://127.0.0.1:8000/api/health
curl http://127.0.0.1:8000/api/documents
```

重新解析已上传文档示例：

```powershell
curl -X POST http://127.0.0.1:8000/api/documents/{document_id}/reparse `
  -F "file=@E:\docs\updated.pdf"
```

问答接口示例：

```powershell
curl -X POST http://127.0.0.1:8000/api/chat `
  -H "Content-Type: application/json" `
  -d "{\"question\":\"建筑防火通用规范 4.1.4 条规定了什么？\"}"
```
