# 07 - 检索与排序

## 模块定位

检索模块负责根据用户问题找到最相关的知识片段，并为回答生成提供有序证据。

主要文件：

- `backend/src/backend/retrieval.py`
- `backend/src/backend/repository.py`
- `backend/src/backend/vector_index.py`
- `backend/src/backend/providers.py`

## 查询类型

`query_type()` 会把问题粗分为：

| 类型 | 触发条件 | 用途 |
| --- | --- | --- |
| `clause` | 包含 `4.1.4` 这类条款号 | 精确查条 |
| `comparison` | 包含对比、比较、新旧、差异等词 | 多文档平衡召回 |
| `summary` | 包含总结、汇总、要求等词 | 主题归纳 |
| `fact` | 默认类型 | 一般事实问答 |

## 检索来源

| 来源 | 说明 |
| --- | --- |
| 条款精确匹配 | 针对明确条款号，优先找正文条款 |
| SQLite FTS5 | 适合规范编号、术语、中文关键词 |
| Qdrant | 适合语义相近问题和补充召回 |
| Rerank | 对融合后的候选证据重新排序 |

## Hybrid Retrieval 技术链路

```text
User Question
  -> QueryPlan
  -> query_type() / focused_query()
  -> comparison document planning
  -> exact clause search
  -> BM25 / SQLite FTS5
  -> Vector Search / Qdrant
  -> Reciprocal Rank Fusion
  -> Reranker
  -> Top-K Evidence
  -> LLM Answer
```

当前融合策略在 `reciprocal_rank_fusion()` 中实现，公式思想是：

```text
score += 1 / (k + rank)
```

它不直接比较 BM25 分数和向量相似度，而是按各路结果排名融合，更适合不同分值尺度的检索源。

## 融合策略

多路结果通过 Reciprocal Rank Fusion 合并。这样可以让不同检索方式各自贡献高排名结果，避免单一路径漏召回。

当未配置外部 Embedding 时，本地哈希向量只作为补召回，关键词检索权重更高。

## 对比类问题

对比类问题采用轻量 Agentic 编排，不引入 LangChain 或 LlamaIndex。核心目标是先把“要对比什么、在哪些文档中对比、还缺哪些维度”拆清楚，再交给检索和 LLM。

当前实现已经从固定规则扩展为“LLM 规划 + 代码执行”的混合方式：

- `AgenticRetriever` 先调用 Planner LLM 输出 JSON 计划。
- Planner LLM 自由生成多条 `query_rewrites` 和 `steps`，不再由代码拼接固定关键词。
- 检索工具限制在 `search_general` 和 `search_in_document`，目标文档必须来自允许文档列表。
- 规划失败、JSON 不合法或模型未配置时，自动回退到本地规则计划。
- 多个检索步骤会并行执行，再统一去重、重排和按文档平衡。

处理流程：

```text
用户问题
  -> query_type() 识别 comparison
  -> AgenticRetriever 生成检索计划
  -> Planner LLM 自由拆解多个检索维度
  -> 并行执行 search_general / search_in_document
  -> 召回结果去重、Rerank、文档间平衡
  -> ChatProvider 按文档分组组织资料包
```

这次改进后，跨文档对比不再只依赖一个笼统问题做单次召回。比如“新旧规范关于工业建筑防火要求的区别”，Planner 会倾向于拆成适用范围、条文要求、数值限制、构造或设施要求等多个维度分别检索，因此更容易命中正文条款和具体表格，而不是反复命中文档开头的总说明、前言或目录。

目标文档来源优先级：

- 前端手动选择的文档。
- 问题中出现的标准号、标题、文件名或版本，如 `GB55037-2022`、`GB50016-2014`。
- “新旧规范”“新版/旧版”等泛化问题，会在 READY 文档中按版本尝试选择新旧文档。

如果用户选择了多份文档，或 planner 从问题中识别出多份目标文档：

- 系统会按文档分别检索
- 再做文档间平衡
- 避免回答只被某一份文档的高分片段占满
- LLM 收到的资料会按文档分组，而不是一组扁平列表

这样回答更容易先判断“是否直接可比”。如果适用对象、限制条件或条款层级不同，回答应明确说明不能直接判断变严或放宽，再分别列出各文档规定。

## 图片资料预览

回答引用会携带 `preview_image_url`，用于前端在回答外层展示最相关的资料预览图。

后端选择逻辑在 `backend/src/backend/service.py::preview_from_parsed()`：

- 优先读取 `storage/parsed/{document_id}/normalized.json` 中当前页、当前块类型附近的 `images`。
- 对图片的 `caption`、`row_context`、`column_context`、`cell_text` 和用户问题做词项匹配。
- 如果命中表格内结构化图片，返回对应图片资源 URL。
- 如果没有结构化 `images`，再回退到解析原始 `content_list.json` 里的 `img_path` / `image_path`。

前端资料预览会优先根据回答正文中的引用编号选择图片，例如回答引用 `[4]` 时优先展示第 4 条资料的预览图；如果回答没有引用编号，才回退到第一条带图资料。

## 维护注意

- `retrieval_final_top_k` 控制最终进入回答生成的证据数量。
- `answer_max_citations` 控制最终返回给前端的引用数量。
- `agentic_parallel_workers` 控制 Agentic 检索步骤并发数，默认 `4`。
- `agentic_planner_llm_enabled` 控制是否启用 LLM JSON 规划；关闭后仍会使用本地规则计划。
- 流式问答的耗时日志写入 `storage/logs/chat-metrics.jsonl`，包括检索耗时、首 token 时间、总耗时、引用数量和问题类型。
- 调整检索参数后，需要用条款查询、主题总结、新旧对比三类问题分别验证。
- 对规范类问题，精确条款和术语匹配通常比泛语义召回更可靠。
- 短标题容易误命中，例如“规范”；文档识别会忽略过短标识，优先使用标准号和版本。

## 关键配置

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `RAG_RETRIEVAL_BM25_TOP_K` | `30` | BM25 候选数 |
| `RAG_RETRIEVAL_DENSE_TOP_K` | `30` | 向量候选数 |
| `RAG_RETRIEVAL_FUSED_TOP_K` | `20` | 融合后候选数 |
| `RAG_RETRIEVAL_FINAL_TOP_K` | `8` | 最终证据数 |
| `RAG_ANSWER_MAX_CITATIONS` | `6` | 返回引用数 |

## 相关测试

```powershell
cd E:\lmq\RAG_ZB
E:\lmq\RAG_ZB\.tools\uv\bin\uv.exe run --project backend pytest backend\tests\test_retrieval.py -q
E:\lmq\RAG_ZB\.tools\uv\bin\uv.exe run --project backend pytest backend\tests\test_providers.py -q
```
