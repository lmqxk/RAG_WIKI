# Agentic RAG

本文说明当前系统中的轻量 Agentic RAG 实现。它主要服务于跨文档对比、资料不足和需要多角度检索的问题。

## 1. 当前定位

当前 Agentic RAG 不是一个具备无限循环推理能力的通用 Agent，而是受控的检索编排层：

- Planner LLM 负责分析问题并输出 JSON 检索计划。
- AgenticRetriever 校验计划，只允许调用内部检索工具。
- 多个检索步骤可以并行执行。
- 结果合并后统一进行一次全局重排序。
- Answer LLM 只根据最终资料包生成回答。

系统没有引入 LangChain 或 LlamaIndex，工具调用、状态和异常回退均由项目代码控制，便于调试和追踪。

## 2. 完整流程

```text
用户问题
  -> QueryPlan 识别问题类型和目标文档
  -> Planner LLM 输出 JSON steps
  -> 校验工具名、Query 和文档范围
  -> 并行执行 BM25 + BGE 向量召回
  -> 合并、去重、跨文档覆盖检查
  -> 资料不足时执行补充检索
  -> 全部候选统一进行一次 Jina Reranker 重排
  -> 跨文档平衡并组织 citations
  -> Answer LLM 流式生成回答
```

## 3. Planner JSON

Planner 输出的计划由 Pydantic 模型校验，核心结构如下：

```json
{
  "intent": "comparison",
  "query_rewrites": ["新规范相关检索语句", "旧规范相关检索语句"],
  "steps": [
    {
      "tool": "search_in_document",
      "query": "由 LLM 自由生成的具体 Query",
      "document_ref": "GB55037-2022",
      "dimension": "技术指标",
      "reason": "检索该文档的对应规定"
    }
  ]
}
```

允许的工具只有：

- `search_general`：在当前目标文档范围内进行混合检索。
- `search_in_document`：针对指定文档进行检索。

`document_ref` 必须能解析为已入库文档的 ID、标准号、标题或文件名，不能调用外部搜索，也不能访问允许文档之外的资料。

## 4. 并行检索

每个 `step` 都会调用已有的 `HybridRetriever`。单个步骤内部包含：

```text
BM25 / SQLite FTS5
  + BGE 中文语义向量 / Qdrant
  -> RRF 融合
```

Agentic 多步骤阶段关闭子步骤重排序，只保留足够大的候选集合。所有步骤完成后再统一调用一次 Jina Reranker，避免每条 Query 重复加载或调用重排模型，也避免提前截断跨文档候选。

并发数由以下配置控制：

```python
agentic_parallel_workers = 4
```

## 5. 补充检索

当前补检是受控回退，不是第二次 Planner LLM 推理循环。以下情况会触发补检：

- 没有召回结果。
- 命中数量低于 `agentic_min_hits`。
- 对比问题只覆盖了一个目标文档。

补检会使用当前问题、聚焦 Query 和问题类型生成候选补检语句，并继续受 `agentic_max_steps` 限制。

因此当前系统具备“一次规划、多步检索、条件补检”，但还不是“LLM 阅读第一轮资料后自主制定第二轮计划”的完整多轮 Agent。后续如果需要增强，应在补检前增加一个结构化的 LLM 资料覆盖判断器。

## 6. 最终资料包和回答

最终候选会经过：

1. 按 chunk 去重。
2. Jina 全局重排序。
3. 对比问题按文档平衡结果。
4. 生成带文档、页码、条款和图片上下文的 citations。

Answer LLM 只接收最终资料 JSON，不接收 Planner 的内部过程，也不应该输出内部图片路径、资料 JSON 或独立引用清单。

前端根据回答正文中的 `[n]` 引用编号关联第 `n` 条资料。回答没有引用编号时，不额外展示图片预览。

## 7. 回退策略

```text
Planner LLM 不可用
  -> 使用本地 QueryPlan 和规则检索步骤

Jina 服务不可用
  -> 使用本地词项、精确匹配和规范正文优先排序

BGE 模型不可用
  -> 健康接口显示 embedding_configured=false
  -> 可显式切换 embedding_backend=hash 做离线调试
```

默认不建议使用哈希向量做正式效果评估。正式检索使用本地 `BAAI/bge-small-zh-v1.5`，输出 512 维向量，存储在独立的 `document_chunks_bge_small_zh_v1_5` Qdrant 集合中。

## 8. 模型加载和预热

问答服务启动时：

- BGE 在 API lifespan 中加载，并用短文本执行一次预热向量化。
- Jina 服务健康检查通过后，由 `scripts/run.py` 发送一次最小重排请求预热。
- PDF-Extract-Kit 只在解析任务期间启动，解析完成后释放其子进程资源。

相关默认配置：

```python
embedding_warmup_on_start = True
local_rerank_warmup_on_start = True
```

BGE 模型和 `transformers` 的首次加载由锁保护，避免 Agent 并行检索线程同时初始化模型。

## 9. 性能日志

流式问答完成后，后端将指标写入：

```text
storage/logs/chat-metrics.jsonl
```

主要字段：

| 字段 | 含义 |
| --- | --- |
| `planner_ms` | Planner LLM 制定检索计划耗时 |
| `recall_ms` | 并行召回和补充检索耗时 |
| `rerank_ms` | 最终一次 Jina 重排序耗时 |
| `answer_llm_ttft_ms` | 检索完成到回答首 token 的耗时 |
| `first_token_ms` | 请求开始到回答首 token 的总耗时 |
| `total_ms` | 整个流式请求耗时 |

这些指标用于判断慢点是在 Planner、召回、重排还是回答模型，而不是只看一个总检索时间。

## 10. 关键配置

```python
agentic_retrieval_enabled = True
agentic_planner_llm_enabled = True
agentic_max_steps = 4
agentic_parallel_workers = 4
agentic_min_hits = 4
embedding_backend = "local"
embedding_dimension = 512
embedding_batch_size = 16
```

最终回答的 `chat_max_tokens` 默认保持 `2048`。实验中 `1024` 在跨文档对比、长条款归纳和资料不足说明场景下容易被截断；Planner 是短 JSON 输出，应单独设置较小的 token 配额。

如果修改 Embedding 模型或维度，必须使用对应的 Qdrant 集合，并对已有文档执行 `reindex`。`reindex` 复用 `normalized.json`，不会重新运行 PDF 解析。
