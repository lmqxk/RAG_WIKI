# Wiki 派生知识层

## 目标

Wiki 层用于把多份规范中的概念、别名、可比较维度和来源关系组织为可导航的知识网络，提升跨文档问答与版本对比的检索质量。

它不是第二套 PDF 解析系统，也不是最终证据库。`storage/parsed`、SQLite 文本块和 Qdrant 向量索引仍是唯一的原文事实来源。

## 分层与存储

```text
storage/
├─ uploads/                         # 原始 PDF
├─ parsed/{document_id}/            # 解析产物：document.md、normalized.json、images
├─ rag.db                           # 文档、任务、chunk、FTS5
├─ qdrant/                          # chunk 向量索引
└─ wiki/                            # 从 parsed/chunk 派生，不重新解析 PDF
   ├─ index.md
   ├─ overview.md
   ├─ log.md
   ├─ documents/{document_id}.md
   ├─ concepts/{concept}.md
   ├─ metadata/{document_id}.json
   └─ graph/related.json
```

`metadata/{document_id}.json` 是 Wiki 页面的结构化来源记录，保存文档标识、解析器、主题、概念、`chunk_id` 锚点和生成方式。Markdown 页面用于人工浏览与交叉链接；Planner 使用轻量 metadata，不把整页 Wiki 或原文全文塞入规划上下文。

## 入库流程

```text
PDF 上传或重新解析
  → PDF-Extract-Kit 解析
  → normalized.json / document.md
  → Chunking
  → SQLite FTS5 + Qdrant
  → WikiManager 读取同一份 ParsedDocument 和 Chunk
  → LLM 结构化分析（失败则回退为确定性摘要）
  → 文档页、概念页、index.md、overview.md
  → 解析显式 [[wikilinks]] 并计算 related 图数据
  → 追加 log.md 事件
```

Wiki 生成位于向量索引完成之后。即使 LLM 不可用或返回无效 JSON，也会保留原有 RAG 入库成功状态，并生成不含概念的基础文档摘要。

## 可追溯规则

- 每个概念必须包含至少一个真实 `chunk_id`。
- LLM 返回的未知 chunk ID 会被丢弃。
- Wiki 不能自行断言“废止”“替代”“现行有效”等规范效力关系；只有输入原文包含直接依据时，后续才可生成带来源、置信度与人工审核状态的候选关系。
- 最终回答必须重新检索原文 chunk，Wiki 只用于概念对齐、文档导航和规划。

## Planner 协同

Planner 原有 `allowed_docs` 之外，新增 `wiki_context`：

```json
[
  {
    "document_id": "...",
    "summary": "文档主题摘要",
    "topics": ["工业建筑", "防火分区"],
    "concepts": ["防火分区", "防火间距"]
  }
]
```

Planner 据此决定目标文档和比较维度，再调用现有 `search_general`、`search_in_document`。Wiki 不直接替代混合检索。

## 索引、概览与日志

- `index.md` 是内容目录：按页面类型列出可导航的文档页和概念页，每次 Wiki 同步重建。
- `overview.md` 是全局摘要：基于全部 metadata 统计文档、概念和主题；配置外部 LLM 时再生成简短摘要与待完善方向，失败则回退为确定性统计。
- `log.md` 是只追加的时间线：当前记录 `ingest`、`reindex` 和 `query`；后续 Lint 也必须通过同一追加接口写入事件，不修改旧记录。

## 显式链接与 related

LLM 在入库分析时可使用 `known_concepts`，输出 `related_concepts`。系统只接受指向既有概念或同轮、有原文锚点的新概念的链接，并把它们写成页面中的显式 `[[wikilinks]]`。

页面写完后，系统将下列信号确定性计算为 `storage/wiki/graph/related.json`，并在页面末尾写入“计算相关”区块：

| 信号 | 权重 | 含义 |
| --- | --- | --- |
| `direct_wikilink` | 3.0 | 页面间存在显式 `[[wikilink]]` |
| `source_overlap` | 4.0 | 页面引用同一 `document_id` |
| `adamic_adar` | 1.5 | 页面拥有共同链接邻居，按邻居度数衰减 |
| `type_affinity` | 1.0 | 已有关联信号时，页面类型相同的附加分 |

`related` 是稳定可重算的导航分数，不是规范事实、效力关系或法律结论。

## 重新解析与增量更新

同一 `document_id` 重新解析后，系统覆盖该文档对应的 metadata 和文档页，再重建受影响概念页与索引。未被当前来源引用的旧概念页不会自动删除，避免在未确认前丢失人工内容；它们不再进入新的 `index.md`。

已有文档可调用现有 `POST /api/documents/{document_id}/reindex`：它会复用 `normalized.json` 重建检索索引，并同步生成该文档的 Wiki 页面，无需再次运行 PDF 解析。

```powershell
curl.exe -X POST "http://127.0.0.1:8000/api/documents/{document_id}/reindex"
```
## 当前范围

当前已经实现：

- 文档页、概念页、索引页和来源 metadata。
- LLM JSON 分析与无 LLM 回退。
- 文档到概念的 chunk 级来源锚点。
- Planner 的轻量 Wiki 上下文。
- 自动更新 `index.md`、`overview.md` 和只追加 `log.md`。
- 显式 `[[wikilinks]]` 与四信号 `related` 图数据。

后续计划：

1. 增加 `search_wiki` 和 `get_related_pages` 工具，并在 Query 时向图中扩展一跳。
2. 为跨文档问题生成可核验的 comparison matrix，并将人工确认结果 promote 为对比页。
3. 增加来源充分时的候选关系页、Lint 和人工审核状态。
4. 增加 Wiki 页的重建、过期标记和前端浏览界面。
