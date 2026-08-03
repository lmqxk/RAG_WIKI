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
  -> Query Understanding
  -> focused_query()
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

如果用户选择了多份文档，并且问题被识别为 `comparison`：

- 系统会按文档分别检索
- 再做文档间平衡
- 避免回答只被某一份文档的高分片段占满

## 维护注意

- `retrieval_final_top_k` 控制最终进入回答生成的证据数量。
- `answer_max_citations` 控制最终返回给前端的引用数量。
- 调整检索参数后，需要用条款查询、主题总结、新旧对比三类问题分别验证。
- 对规范类问题，精确条款和术语匹配通常比泛语义召回更可靠。

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
```
