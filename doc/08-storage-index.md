# 08 - 存储与索引

## 模块定位

存储与索引模块负责保存文档元数据、任务状态、文本块、全文检索数据和向量索引数据。

主要文件：

- `backend/src/backend/db.py`
- `backend/src/backend/repository.py`
- `backend/src/backend/vector_index.py`

## SQLite

默认数据库路径：

```text
storage/rag.db
```

核心表：

| 表 | 作用 |
| --- | --- |
| `documents` | 文档元数据和处理状态 |
| `jobs` | 后台任务状态和错误信息 |
| `chunks` | 切分后的文本块 |
| `chunks_fts` | SQLite FTS5 全文索引 |

SQLite 启用 WAL，便于读写并发和本地开发。

## SQLite Schema

表结构在 `backend/src/backend/db.py` 的 `SCHEMA` 常量中维护。

核心索引：

```sql
CREATE INDEX IF NOT EXISTS idx_documents_status ON documents(status);
CREATE INDEX IF NOT EXISTS idx_jobs_document ON jobs(document_id);
CREATE INDEX IF NOT EXISTS idx_chunks_document ON chunks(document_id);
CREATE INDEX IF NOT EXISTS idx_chunks_clause ON chunks(clause_no);
```

FTS5 虚拟表：

```sql
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    chunk_id UNINDEXED,
    document_id UNINDEXED,
    clause_no,
    chapter_path,
    terms,
    tokenize = 'unicode61'
);
```

## 全文索引

`repository.search_terms()` 会为文本生成适合 FTS5 的检索词：

- 英文、数字、规范编号保留为词项
- 中文使用二元字串
- 条款号、章节路径、正文共同进入索引

## Qdrant

默认索引路径：

```text
storage/qdrant
```

集合名称：

```text
document_chunks
```

每个文本块写入一个 point：

- point id：`chunk.id`
- vector：由 `EmbeddingProvider` 生成
- payload：`document_id`

Qdrant collection 配置：

| 项 | 值 |
| --- | --- |
| collection | `document_chunks` |
| distance | `COSINE` |
| vector size | `RAG_EMBEDDING_DIMENSION` |

## 重建写入

重建某份文档索引时：

- SQLite 删除该文档旧 `chunks` 和 `chunks_fts`
- SQLite 写入新文本块和全文索引
- Qdrant 删除该文档旧 points
- Qdrant 写入新 vectors

## 索引重建方法

当修改了切片、表格行解析、检索词生成、重排逻辑或向量写入逻辑后，已上传文档不会自动应用新规则。需要先重启后端进程，让新代码生效，再对目标文档调用重建索引接口。

单文档重建：

```powershell
curl.exe -X POST http://127.0.0.1:8000/api/documents/{document_id}/reindex
```

示例：

```powershell
curl.exe -X POST http://127.0.0.1:8000/api/documents/b7c7d518-3b70-4122-8f07-1faab100fc3a/reindex
```

重建流程会复用：

```text
storage/parsed/{document_id}/normalized.json
```

它不会重新解析 PDF，只会重新执行：

```text
normalized.json
  -> build_chunks()
  -> SQLite chunks / chunks_fts
  -> Qdrant document_chunks
```

如果改的是 PDF 解析器、MinerU 输出转换、OCR 或 `normalized.json` 生成逻辑，只调用 `reindex` 不够，需要重新解析文档：

```powershell
curl -X POST http://127.0.0.1:8000/api/documents/{document_id}/reparse `
  -F "file=@E:\docs\updated.pdf"
```

排查是否仍是旧索引时，可以查看该文档当前 chunk 数量和内容：

```powershell
cd E:\lmq\RAG_ZB
.\backend\.venv\Scripts\python.exe -c "import sqlite3; con=sqlite3.connect('storage/rag.db'); doc='b7c7d518-3b70-4122-8f07-1faab100fc3a'; print(con.execute('select count(*) from chunks where document_id=?',(doc,)).fetchone()[0])"
```

注意：Qdrant Local 同一时间只允许一个后端进程写入。重建索引前确认没有多个后端同时指向同一个 `storage/qdrant`。

## 维护注意

- 数据库结构变更属于高风险操作，需要先确认。
- Qdrant 本地存储同一时间只能被一个进程写入，重复启动后端可能遇到锁冲突。
- 如果 Embedding 维度改变，现有 Qdrant 集合维度不匹配，需要规划重建索引。
- 不要手动编辑 `storage` 下的数据文件，除非已经备份并明确知道影响范围。

## 数据排查

只读检查 SQLite：

```powershell
cd E:\lmq\RAG_ZB
sqlite3 storage\rag.db ".tables"
sqlite3 storage\rag.db "select id,title,status,page_count from documents;"
```

不要在生产数据上直接执行 `DELETE`、`DROP`、`UPDATE`，除非已经确认目标和备份。
