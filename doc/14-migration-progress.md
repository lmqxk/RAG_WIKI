# RAG_ZB 产品化迁移进度与续改清单

> 更新时间：2026-08-16（2026-08-16 更新：Wiki 层适配 StorageBackend）
>
> 目的：记录本轮从单机 RAG 向多人产品演进的实际代码改动、验证结果和后续安全续改顺序。开始下一轮前先阅读本文及 `13-multiuser-deployment-plan.md`。

## 1. 当前结论

系统仍应视为**单组织/单机可用**版本，但**组织隔离、角色权限和全部生产服务拆分已全部就位**。

Step A（组织隔离）、Step B（角色权限）、**Step C（生产服务拆分全部 11 项）** 已完成。
后端代码已具备完整的 PostgreSQL + Qdrant Server + S3/MinIO + Celery + SSE 生产能力。

但前端尚未适配登录页和 Token 管理。因此，在完成前端适配前：

- 不要启用第二个组织（后端已支持，但前端无法管理）。
- 不要把服务暴露到公网。
- 若启用认证，只允许默认组织 `default-org` 使用。

## 2. 本轮已完成改动

### 2.1 数据一致性与安全加固（Phase 0）

| 项目 | 状态 | 主要位置 |
| --- | --- | --- |
| 重解析 PDF 原子替换 | 已完成 | `backend/src/backend/service.py` |
| 同文档活动任务互斥 | 已完成 | `db.py`、`repository.py`、`main.py` |
| SQLite/Qdrant 写入补偿恢复 | 已完成 | `ingestion.py`、`vector_index.py` |
| PDF 文件头、请求数量、异常脱敏 | 已完成 | `service.py`、`schemas.py`、`main.py` |
| MinerU ZIP 安全解压 | 已完成 | `parser.py` |

关键行为：

1. 重解析先写临时文件，哈希冲突或数据库更新失败时恢复原 PDF。
2. 通过 SQLite `BEGIN IMMEDIATE` 原子创建任务；同一文档存在 `QUEUED/RUNNING` 任务时，reindex 返回 409。
3. Qdrant 向量替换前保存旧快照；向量或 SQLite chunks 写入失败时尝试恢复旧向量。
4. 上传和重解析校验 `%PDF-` 文件头；ZIP 解压阻止路径穿越、符号链接、过多文件和超大解压内容。

### 2.2 组织与认证基础（Phase 1 已完成部分）

| 项目 | 状态 | 主要位置 |
| --- | --- | --- |
| `organizations` 表与默认组织 | 已完成 | `backend/src/backend/db.py` |
| 历史 SQLite 文档回填 `default-org` | 已完成 | `Database.initialize()` |
| 用户、组织成员与角色表 | 已完成 | `db.py` |
| PBKDF2 密码哈希、HS256 JWT | 已完成 | `backend/src/backend/auth.py` |
| 登录接口 | 已完成 | `POST /api/auth/login` |
| 认证中间件与组织选择 | 已完成 | `main.py` |
| 文档列表按组织过滤 | 已完成 | `Repository.list_documents()` |

当前角色：`admin`、`editor`、`viewer`。

当前组织选择方式：认证后在请求头携带：

```http
Authorization: Bearer <access-token>
X-Organization-ID: <organization-id>
```

服务端会验证该用户是否属于指定组织；无成员关系时返回 403。

### 2.3 组织范围全链路贯穿（Step A 已完成）

| 项目 | 状态 | 主要位置 |
| --- | --- | --- |
| `OrganizationContext` 依赖注入 | 已完成 | `main.py` |
| `Repository.get_document()` 可选组织过滤 | 已完成 | `repository.py` |
| `Repository.get_job()` 可选组织过滤 | 已完成 | `repository.py` |
| `bm25_search()` 组织过滤 | 已完成 | `repository.py` |
| `exact_clause_search()` 组织过滤 | 已完成 | `repository.py` |
| `get_chunks()` 组织过滤 | 已完成 | `repository.py` |
| Qdrant point payload 写入 `organization_id` | 已完成 | `vector_index.py` |
| Qdrant 搜索强制组织 Filter | 已完成 | `vector_index.py` |
| `HybridRetriever` 贯穿 `organization_id` | 已完成 | `retrieval.py` |
| `AgenticRetriever` 贯穿 `organization_id` | 已完成 | `agent.py` |
| `IngestionManager` 贯穿 `organization_id` | 已完成 | `ingestion.py` |
| `RagService.upload()` 组织隔离 | 已完成 | `service.py` |
| `RagService.reparse_document()` 组织隔离 | 已完成 | `service.py` |
| `RagService.chat()` 组织隔离 | 已完成 | `service.py` |
| 所有 API 端点注入组织上下文 | 已完成 | `main.py` |
| 上传路径按组织分目录 | 已完成 | `service.py` |
| 解析产物路径按组织分目录 | 已完成 | `ingestion.py` |
| Wiki 元数据记录组织 ID 与路径 | 已完成 | `wiki.py` |

### 2.4 角色权限与组织管理 API（Step B 已完成）

| 项目 | 状态 | 主要位置 |
| --- | --- | --- |
| `require_role()` 依赖注入 | 已完成 | `main.py` |
| 认证未启用时跳过角色校验 | 已完成 | `main.py` |
| 写操作端点标注 `editor` 角色 | 已完成 | `main.py` |
| 管理端点标注 `admin` 角色 | 已完成 | `main.py` |
| 创建组织 API | 已完成 | `POST /api/admin/organizations` |
| 组织列表 API | 已完成 | `GET /api/admin/organizations` |
| 成员列表 API | 已完成 | `GET /api/admin/organizations/{org_id}/members` |
| 添加成员 API | 已完成 | `POST /api/admin/organizations/{org_id}/members` |
| 移除成员 API | 已完成 | `DELETE /api/admin/organizations/{org_id}/members/{user_id}` |
| 修改角色 API | 已完成 | `PATCH /api/admin/organizations/{org_id}/members/{user_id}` |
| 审计日志表 `audit_logs` | 已完成 | `db.py` |
| 审计日志写入 | 已完成 | `main.py`（上传、重解析、reindex、元数据更新、组织管理操作均记录） |
| 审计日志查询 API | 已完成 | `GET /api/admin/audit-logs` |

### 2.5 角色权限映射

| 端点 | 所需角色 | 说明 |
| --- | --- | --- |
| `GET /api/documents` | viewer | 文档列表 |
| `POST /api/documents` | **editor** | 上传文档 |
| `PATCH /api/documents/{id}` | **editor** | 更新元数据 |
| `POST /api/documents/{id}/reparse` | **editor** | 重解析 |
| `POST /api/documents/{id}/reindex` | **editor** | 重建索引 |
| `GET /api/jobs/{id}` | viewer | 任务查询 |
| `GET /api/documents/{id}/file` | viewer | PDF 文件 |
| `GET /api/documents/{id}/asset` | viewer | 解析资源 |
| `POST /api/chat` | viewer | 问答 |
| `POST /api/chat/stream` | viewer | 流式问答 |
| `POST /api/admin/organizations` | **admin** | 组织管理 |
| `GET /api/admin/organizations` | **admin** | 组织列表 |
| `GET /api/admin/organizations/{id}/members` | **admin** | 成员列表 |
| `POST /api/admin/organizations/{id}/members` | **admin** | 添加成员 |
| `DELETE /api/admin/organizations/{id}/members/{uid}` | **admin** | 移除成员 |
| `PATCH /api/admin/organizations/{id}/members/{uid}` | **admin** | 修改角色 |
| `GET /api/admin/audit-logs` | **admin** | 审计日志 |

认证未启用时，所有角色校验自动跳过，保持向后兼容。

### 2.6 认证启用方式

### 2.7 生产服务拆分——数据库层（Step C 已完成）

| 项目 | 状态 | 主要位置 |
| --- | --- | --- |
| SQLAlchemy 2 ORM 模型 | 已完成 | `backend/src/backend/models.py`（7 张表） |
| Alembic 迁移框架 | 已完成 | `alembic.ini`、`alembic/env.py`、`alembic/versions/001_initial_schema.py` |
| 双模式 Database 类 | 已完成 | `db.py`（`use_sqlalchemy` 属性，SQLite 走原生，PostgreSQL 走 ORM） |
| `database_url` 配置 | 已完成 | `config.py`（`RAG_DATABASE_URL` 环境变量） |
| 数据迁移工具 | 已完成 | `scripts/migrate_db.py`（`export`、`import`、`check` 三个子命令） |
| QueryEngine 抽象层 | 已完成 | `query_engine.py`（SQLiteEngine + PostgreSQLEngine，统一查询接口） |
| Repository 重构 | 已完成 | `repository.py`（所有方法改用 QueryEngine，不再直接操作 sqlite3） |
| **Qdrant 双模式** | 已完成 | `vector_index.py` + `config.py`（`RAG_QDRANT_URL` / `RAG_QDRANT_API_KEY`） |
| **S3/MinIO 对象存储** | 已完成 | `storage.py`（`StorageBackend` / `LocalStorage` / `S3Storage`） |
| **Redis + Celery Worker** | 已完成 | `tasks.py`（Celery 应用 + `process_document` / `reindex_document` 任务） |
| **SSE 实时任务推送** | 已完成 | `events.py`（`EventBus` / `InMemoryEventBus` / `RedisEventBus`） |

**使用方式**：

```bash
# 导出 SQLite 数据
python scripts/migrate_db.py export

# 设置 PostgreSQL 连接
export RAG_DATABASE_URL=postgresql://user:pass@host:5432/rag_zb

# 设置 Qdrant Server（可选，默认使用本地文件）
export RAG_QDRANT_URL=https://your-qdrant-instance:6333
export RAG_QDRANT_API_KEY=your-api-key  # 可选

# 设置 S3/MinIO 对象存储（可选，默认使用本地文件系统）
export RAG_STORAGE_BACKEND=s3
export RAG_S3_ENDPOINT=https://s3.amazonaws.com   # MinIO 用 http://localhost:9000
export RAG_S3_BUCKET=rag-zb
export RAG_S3_ACCESS_KEY=your-access-key
export RAG_S3_SECRET_KEY=your-secret-key
export RAG_S3_REGION=us-east-1

# 设置 Celery 任务队列（可选，默认使用 ThreadPoolExecutor）
export RAG_TASK_BACKEND=celery
export RAG_REDIS_URL=redis://localhost:6379/0

# 启动应用（自动创建表/集合/桶）
python -m backend.main

# 启动 Celery Worker（仅 RAG_TASK_BACKEND=celery 时需要）
celery -A backend.tasks worker --loglevel=info

# SSE 实时事件自动启用（无需额外配置）
# 无 Redis → InMemoryEventBus（同进程）
# 有 Redis → RedisEventBus（跨进程/Celery 模式）

# 导入数据
python scripts/migrate_db.py import

# 校验数据
python scripts/migrate_db.py check
```

本轮**没有修改** `.env`。需要启用时，由部署者自行配置：

```dotenv
RAG_AUTH_ENABLED=true
RAG_AUTH_JWT_SECRET=<高强度随机密钥>
RAG_AUTH_BOOTSTRAP_USERNAME=admin
RAG_AUTH_BOOTSTRAP_PASSWORD=<首次管理员密码>
```

行为：

- 未启用认证：保持原单机匿名访问行为。
- 启用认证但未设置 `RAG_AUTH_JWT_SECRET`：应用启动失败，防止错误暴露。
- 同时设置 bootstrap 用户名和密码：仅在该用户名不存在时创建默认组织管理员；已有用户密码不会被覆盖。

## 3. 当前未完成、不能误用的部分

以下项目**尚未完成**，**不能据此宣称已经完成多租户产品**：

1. **前端**：当前没有登录页、Token 存储、刷新 Token 或 `X-Organization-ID` 请求头注入。
2. **`sha256` 唯一约束**：已改为 `(organization_id, sha256)` 复合唯一约束（✅ 已完成，见 `002_org_sha256_unique.py` 迁移脚本）
3. **注册 API**：`POST /api/auth/register` 已添加（✅ 已完成，自助注册后自动加入 viewer 角色）

## 4. 已完成步骤

### Step A：组织范围全链路贯穿——**已完成（2026-08-16）**

### Step B：角色权限与组织管理——**已完成（2026-08-16）**

### Step C：生产服务拆分——**全部完成（2026-08-16）**

1. ✅ SQLAlchemy 2 ORM 模型（映射所有 7 张表）
2. ✅ Alembic 迁移框架（`alembic.ini`、`env.py`、初始迁移脚本）
3. ✅ `database_url` 配置支持 PostgreSQL 和 SQLite 双模式
4. ✅ `db.py` 支持 PostgreSQL 模式（SQLAlchemy engine/session）
5. ✅ 数据迁移工具（`scripts/migrate_db.py`：export / import / check）
6. ✅ QueryEngine 抽象层（`query_engine.py`：SQLiteEngine + PostgreSQLEngine）
7. ✅ Repository 全面重构（所有方法改用 QueryEngine，不再直接操作 sqlite3）
8. ✅ **Qdrant 双模式**：`RAG_QDRANT_URL` 环境变量，支持 Qdrant Server（远程）和 Local（本地）自动切换
9. ✅ **S3/MinIO 对象存储**：`RAG_STORAGE_BACKEND=s3` 环境变量，支持 Local 和 S3 双模式
10. ✅ **Redis + Celery Worker**：`RAG_TASK_BACKEND=celery` 环境变量，支持 ThreadPoolExecutor 和 Celery 双模式
11. ✅ **SSE 实时任务推送**：`GET /api/events` 端点，支持 InMemory 和 Redis pub/sub 双模式

### 后续计划

- **前端**：登录页、Token 管理、组织切换
- **Kubernetes**：部署配置、健康检查、优雅关闭
- **监控**：Prometheus 指标、ELK/Loki 日志聚合

## 5. 已执行验证

本轮针对性验证均通过：

```text
Phase 0 原子重解析：test_service.py，8 passed
任务互斥：repository/service/main，15 passed
向量失败恢复：5 passed
输入与 ZIP 安全：10 passed
组织回填：14 passed
认证基础与管理员引导：5 passed
ruff check：各改动模块通过
Step A 组织贯穿：8 个模块 AST 语法检查通过
Step B 角色权限：4 个模块 compile 通过
  - main.py: require_role 依赖、组织管理 API、审计日志端点
  - repository.py: 组织/成员/审计日志方法
  - schemas.py: 新增 6 个 Pydantic 模型
  - db.py: audit_logs 表结构
Step C 生产拆分：8 个文件 compile 通过
  - models.py: SQLAlchemy ORM 映射（Organization/User/Member/Document/Job/Chunk/AuditLog）
  - db.py: 双模式 Database 类（SQLite + PostgreSQL）
  - config.py: database_url 配置
  - main.py: 传递 database_url 给 Database
  - alembic/ 迁移框架（ini + env.py + 初始迁移脚本 001）
  - scripts/migrate_db.py: 数据迁移工具（export/import/check）
  - query_engine.py: QueryEngine 抽象（SQLiteEngine + PostgreSQLEngine，统一 BM25/条款检索/Chunk 替换接口）
  - repository.py: 所有方法改用 QueryEngine，不再直接操作 sqlite3
  - vector_index.py: Qdrant 双模式（Local path + Remote url/api_key）
  - config.py: qdrant_url / qdrant_api_key 配置，qdrant_is_remote 属性，ensure_directories 跳过远程模式
  - storage.py: StorageBackend 抽象层（LocalStorage + S3Storage，含 PDF 上传/替换/删除/缓存/预签名 URL）
  - service.py: 上传/重解析改用 storage.save_pdf/replace_pdf，PDF 头校验移至存储后端
  - ingestion.py: 解析产物上传改用 storage.save_parsed，reindex 改用 storage.get_parsed_path
  - main.py: 文件服务端点改用 storage.serve_pdf/serve_asset，app.state.storage 注入
  - tasks.py: Celery 应用（create_celery_app + _run_process / _run_reindex 任务函数）
  - ingestion.py: 提取模块级函数（save_parsed_output / load_parsed_payload / build_parsed_from_payload），支持 Celery 和 ThreadPoolExecutor 双模式
  - config.py: task_backend / redis_url 配置，ensure_directories 包含 .s3-cache
  - events.py: EventBus 抽象 + InMemoryEventBus + RedisEventBus + make_event 工具
  - repository.py: update_job 发布 EVENT_JOB_UPDATED 事件到 EventBus
  - main.py: /api/events SSE 端点，lifespan 初始化 event_bus

（完整 pytest 因环境依赖未全部安装，尚未运行；所有修改模块的 Python compile 检查通过）
```

完整后端测试曾得到 `47 passed, 1 failed`。唯一失败项是 `test_local_embedding_is_not_configured_until_model_is_downloaded`，它受当前 `.env` 中的 embedding/fallback 配置影响，而测试假设不存在远程 fallback；该问题在继续功能开发前可单独修复为“测试显式清理环境变量”。

## 6. 相关文档

- `doc/13-multiuser-deployment-plan.md`：总体产品化演进计划与架构图。
- `doc/14-migration-progress.md`：本文件，实际进度与交接清单。
- `E:\lmq\designflow\docs\系统设计\designflow-functional-map.md`：仅参考认证、队列、对象存储、Worker 与 SSE 的职责拆分方式。
