# Wiki 派生知识层

主要文件：

- `backend/src/backend/wiki.py`
- `backend/src/backend/agent.py`（`search_wiki` 工具）
- `scripts/rebuild_wiki.py`

## 模块定位

Wiki 层把多份规范中的概念、别名、可比较维度和来源关系组织为**可导航的知识网络**，服务于跨文档问答的检索规划和概念对齐。

边界很明确：它不是第二套 PDF 解析系统，也不是最终证据库。`storage/parsed`、SQLite 文本块和 Qdrant 向量索引仍是**唯一的原文事实来源**——最终回答必须重新检索原文 chunk，Wiki 只用于概念对齐、文档导航和规划。

## 分层与存储

```text
storage/
├─ uploads/                         # 原始 PDF
├─ parsed/{document_id}/            # 解析产物：document.md、normalized.json、images
├─ rag.db                           # 文档、任务、chunk、FTS5
├─ qdrant-server/                   # chunk 向量索引（Docker 模式）
└─ wiki/                             # 从 parsed/chunk 派生，不重新解析 PDF
   ├─ index.md                       # 内容目录（每次 Wiki 同步重建）
   ├─ overview.md                    # 全局摘要与统计
   ├─ log.md                         # 只追加事件时间线
   ├─ documents/{document_id}.md     # 文档页
   ├─ concepts/{concept}.md          # 概念页
   ├─ metadata/{document_id}.json    # 结构化来源记录
   └─ graph/related.json            # 概念关系图数据
```

`metadata/{document_id}.json` 是 Wiki 页面的结构化来源记录，保存文档标识、解析器、主题、概念、`chunk_id` 锚点和生成方式。Markdown 页面用于人工浏览与交叉链接；Planner 只使用轻量 metadata，不把整页 Wiki 或原文全文塞入规划上下文。

## 入库流程

```text
PDF 上传或重新解析
  -> 解析 / normalized.json / document.md
  -> Chunking -> SQLite FTS5 + Qdrant
  -> WikiManager 读取同一份 ParsedDocument 和 Chunk（复用数据库 chunk ID）
  -> LLM 结构化分析（整文档分批，失败回退确定性摘要）
  -> 文档页、概念页、index.md、overview.md
  -> 解析显式 [[wikilinks]] 并计算 related 图数据
  -> 追加 log.md 事件
```

Wiki 生成位于向量索引完成之后。即使 LLM 不可用或返回无效 JSON，RAG 入库仍然成功，并生成不含概念的基础文档摘要——Wiki 失败不阻断主链路。

## 可追溯规则

- 每个概念必须包含至少一个真实 `chunk_id`；LLM 返回的未知 chunk ID 会被丢弃。
- **Wiki 重建复用数据库中已有的 chunk ID**，不重新调用 `build_chunks()` 生成新 UUID——否则锚点与引用全部断裂（这是曾经踩过的坑）。
- Wiki 不能自行断言"废止""替代""现行有效"等规范效力关系；只有输入原文包含直接依据时，才可生成带来源、置信度与人工审核状态的候选关系。
- Planner 拿 Wiki 上下文做规划，最终证据仍来自混合检索的原文 chunk。

## Planner 协同

Planner 在 `allowed_docs` 之外接收 `wiki_context`：

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

`search_wiki` 工具（`agent.py`）按概念名查询 Wiki 概念页关联的 chunk 集合，作为混合检索之外的语义关联召回通道（每计划最多调用一次）。

## 显式链接与 related 图

LLM 在入库分析时可使用 `known_concepts` 输出 `related_concepts`；系统只接受指向既有概念（或有原文锚点的新概念）的链接，写成页面显式 `[[wikilinks]]`。

页面写完后，系统将下列信号确定性计算为 `storage/wiki/graph/related.json`：

| 信号 | 权重 | 含义 |
| --- | --- | --- |
| `direct_wikilink` | 3.0 | 页面间存在显式 `[[wikilink]]` |
| `source_overlap` | 4.0 | 页面引用同一 `document_id` |
| `adamic_adar` | 1.5 | 页面拥有共同链接邻居，按邻居度数衰减 |
| `type_affinity` | 1.0 | 已有关联信号时，页面类型相同的附加分 |

`related` 是稳定可重算的**导航分数**，不是规范事实、效力关系或法律结论。跨文档相关（`source_overlap` 之外的信号）与同文档相关区分计分，避免同一文档的概念对淹没真正的跨文档关系。

## 索引、概览与日志

| 文件 | 性质 | 说明 |
| --- | --- | --- |
| `index.md` | 重建 | 按页面类型列出可导航的文档页和概念页 |
| `overview.md` | 重建 | 基于 metadata 统计文档、概念和主题；LLM 可用时生成摘要与待完善方向 |
| `log.md` | **只追加** | 记录 `ingest`、`reindex`、`query` 事件；Lint 类扩展必须走同一追加接口，不修改旧记录 |

## 重新解析与增量更新

同一 `document_id` 重新解析后，系统覆盖该文档的 metadata 和文档页，再重建受影响概念页与索引。未被当前来源引用的旧概念页**不会自动删除**（避免未确认前丢失人工内容），只是不再进入新的 `index.md`。

已有文档调用 `POST /api/documents/{document_id}/reindex`：复用 `normalized.json` 重建检索索引，并同步刷新该文档的 Wiki 页面，无需重新解析 PDF。全库 Wiki 重建用：

```powershell
backend\.venv\Scripts\python.exe scripts\rebuild_wiki.py
```

## 运维思考

| 维度 | 考量 |
| --- | --- |
| 时间 | LLM 概念分析按文档分批（`wiki_analysis_max_batches` 默认 6 批），单文档约几十秒（受 LLM 速度限制）；确定性 fallback 路径秒级完成 |
| token 成本 | 概念抽取必须处理整份文档而非 8-chunk 采样（采样会导致跨文档关联残缺）；分批控制单次 LLM 上下文 |
| 一致性 | Wiki 页面锚点 = 数据库 chunk ID；切分规则变更（ID 变化）后必须同步重建 Wiki，否则锚点 404 |
| 幂等 | 重建是"删除该文档派生页 + 重写"而非追加，多次执行结果一致 |
| 降级 | LLM 失败时生成确定性摘要（无概念，仅文档页），RAG 问答完全不受影响 |

## 当前范围与后续计划

已实现：

- 文档页、概念页、索引页和来源 metadata
- LLM JSON 分析与无 LLM 回退（整文档分批处理）
- 文档到概念的 chunk 级来源锚点
- Planner 轻量 Wiki 上下文 + `search_wiki` 工具
- 自动更新 `index.md`、`overview.md` 和只追加 `log.md`
- 显式 `[[wikilinks]]` 与四信号 `related` 图数据

后续计划：

1. 查询时向 related 图扩展一跳，扩大概念关联召回
2. 为跨文档问题生成可核验的 comparison matrix，人工确认后 promote 为对比页
3. 来源充分时的候选关系页、Lint 和人工审核状态
4. Wiki 页的过期标记与前端管理界面

## 相关测试

```powershell
cd E:\lmq\RAG_ZB
E:\lmq\RAG_ZB\.tools\uv\bin\uv.exe run --project backend pytest backend\tests\test_wiki.py -q
```
