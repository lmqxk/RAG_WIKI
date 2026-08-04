# 05 - 文档入库

## 模块定位

文档入库模块负责编排 PDF 从上传文件到可检索知识库的全过程，包括任务状态、解析、切分、索引写入和失败恢复。

主要文件：

- `backend/src/backend/ingestion.py`
- `backend/src/backend/service.py`
- `backend/src/backend/repository.py`

## 技术实现

| 能力 | 实现 |
| --- | --- |
| 异步后台任务 | `ThreadPoolExecutor` |
| 上传分块读取 | `UploadFile.read(1024 * 1024)` |
| 重复文件识别 | SHA-256 |
| 任务状态 | SQLite `jobs` 表 |
| 文档状态 | SQLite `documents` 表 |
| 结构化产物 | `normalized.json` + `document.md` |
| 索引写入 | SQLite FTS5 + Qdrant Local |

## 上传入口

`RagService.upload()` 负责：

- 校验文件扩展名必须是 `.pdf`
- 按 1 MB 分块读取上传内容
- 计算 SHA-256，识别重复文件
- 按文件名推断标题、规范编号、版本
- 创建 `documents` 记录
- 创建 `jobs` 记录
- 提交后台入库任务

## 入库任务流程

```text
QUEUED
  -> RUNNING / preflight
  -> RUNNING / parsing
  -> RUNNING / chunking
  -> RUNNING / indexing
  -> COMPLETED / completed
```

失败时：

```text
FAILED / failed
```

文档状态同步变化：

```text
QUEUED -> PARSING -> INDEXING -> READY
```

失败时文档状态为 `FAILED`，错误信息写入 `documents.error` 和 `jobs.error`。

## Pipeline 细节

`IngestionManager._process()` 的关键阶段：

1. `preflight`：读取任务和文档记录，检查 PDF 文件路径。
2. `parsing`：调用 `DocumentParser.parse()`，实时更新解析进度。
3. `save_parsed`：落盘 `normalized.json` 和 `document.md`。
4. `chunking`：调用 `build_chunks()` 生成 `Chunk`。
5. `repository.replace_chunks()`：重写 SQLite chunks 和 FTS5。
6. `vector_index.replace_document()`：重写 Qdrant points。
7. `READY`：文档进入可检索状态。

## 任务恢复

应用启动时会调用 `IngestionManager.recover()`：

- 把上次异常退出时仍为 `RUNNING` 的任务重新置为 `QUEUED`
- 按创建时间重新提交等待任务

## 重新解析

`POST /api/documents/{document_id}/reparse` 用于替换已上传 PDF，并对同一个文档 ID 重新执行完整入库链路：

```text
替换 storage/uploads/{document_id}.pdf
  -> 重置 documents 状态为 QUEUED
  -> 创建新的 jobs 记录
  -> 调用 IngestionManager.submit()
  -> 重新解析、切分、写入 SQLite FTS5 和 Qdrant
```

如果该文档已有 `QUEUED` 或 `RUNNING` 任务，接口会返回冲突错误，避免两个后台任务同时改同一份文档。

## 重建索引

`POST /api/documents/{document_id}/reindex` 会复用：

```text
storage/parsed/{document_id}/normalized.json
```

它不会重新解析 PDF，只会重新执行切分和索引写入，适合调整切分规则后的快速验证。

## 图片结构入库

`normalized.json` 会保存每个 `PageBlock` 的 `images` 字段。这个字段用于记录图片路径、caption、表格行上下文、列上下文和单元格文本。

`IngestionManager._save_parsed()` 写入：

```json
{
  "page": 2,
  "text": "...",
  "type": "table",
  "images": [
    {
      "path": "images/f078.jpg",
      "caption": "轻型木桁架屋顶截面",
      "row_context": "屋顶承重构件",
      "column_context": "截面图和结构厚度或截面最小尺寸(mm)",
      "cell_text": "..."
    }
  ]
}
```

`reindex` 会读取已有 `normalized.json` 里的 `images` 并重新切分、索引。若旧文档的 `normalized.json` 里没有 `images`，需要走 `reparse` 重新解析 PDF。

## 维护注意

- 入库线程数由 `RAG_MAX_WORKERS` 控制，当前默认是 1，避免大 PDF 并发时抢占内存。
- `replace_chunks()` 和 `replace_document()` 是重建索引的核心配对，二者要保持一致。
- 如果调整解析结果格式，需要同步更新 `_save_parsed()` 和 `_reindex()` 的读取逻辑，尤其是 `PageBlock.images` 这类结构化字段。
- Qdrant Local 有文件锁，开发时不要同时启动多个后端进程指向同一个 `storage/qdrant`。
