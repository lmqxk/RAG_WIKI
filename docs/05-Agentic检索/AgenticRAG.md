# Agentic 检索与问答

主要文件：

- `backend/src/backend/agent.py`
- `backend/src/backend/prompt.py`（Planner 提示词）

## 模块定位

Agentic RAG 是系统中的**受控检索编排层**，主要服务于跨文档对比、资料不足和需要多角度检索的问题。它不是具备无限循环推理能力的通用 Agent：Planner LLM 制定一次计划，检索工具受白名单约束，结果统一重排后交给 Answer LLM。

系统没有引入 LangChain 或 LlamaIndex——工具调用、状态和异常回退均由项目代码控制，便于调试、追踪和单元测试。

## 为什么自研而不上框架

| 方案 | 优势 | 劣势 | 结论 |
| --- | --- | --- | --- |
| 自研编排（采用） | 全链路可控可测；无隐藏抽象；回退策略明确 | 编排逻辑要自己维护 | **采用**：需求收敛（对比/归纳/查条三类），自研代码量小 |
| LangChain | 生态全 | 抽象层厚，调试黑盒化；版本变动频繁 | 不采用 |
| LlamaIndex | RAG 范式完整 | 定制混合检索和文档平衡逻辑仍需绕开框架 | 不采用 |

## 完整流程

```text
用户问题
  -> QueryPlan 识别问题类型和目标文档
  -> Planner LLM 输出 JSON steps
  -> 校验工具名、Query 和文档范围
  -> 并行执行混合检索（BM25 + Qwen3 向量，RRF）
  -> 合并、去重、跨文档覆盖检查
  -> 资料不足时执行补充检索
  -> 全部候选统一一次 Qwen3-Reranker 重排
  -> 跨文档平衡并组织 citations
  -> Answer LLM 流式生成回答
```

## Planner 的输入边界

Planner 不直接阅读 PDF 或全部 Wiki 页面，只接收三类轻量上下文：

1. **用户问题**：原始问题、比较意图、用户指定的文档范围。
2. **可用文档目录**：状态为 `READY` 的文档（`id`、`title`、`standard_no`、`version`、`filename`）。
3. **Wiki metadata**：`storage/wiki/metadata/{document_id}.json` 的 `summary`、`topics`、概念名列表。

这样设计的原因：控制 token 和规划耗时；避免把 Wiki 摘要误当成最终证据（Wiki 只用于概念对齐和文档导航，最终回答必须回到原文 chunk 检索）。

## Planner JSON 计划

计划由 Pydantic 模型校验：

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

允许的工具白名单（`agent.py::ALLOWED_TOOLS`）：

| 工具 | 作用 |
| --- | --- |
| `search_general` | 在当前目标文档范围内混合检索 |
| `search_in_document` | 针对指定文档检索（`document_ref` 必须解析为已入库文档） |
| `search_wiki` | 按概念名查 Wiki 概念页关联的 chunk 集合，扩大语义关联召回（每计划最多一次） |

`document_ref` 必须能解析为已入库文档的 ID、标准号、标题或文件名，不能调用外部搜索，也不能访问允许文档之外的资料——这是防止 Planner 幻觉的核心约束。

## 并行检索

每个 `step` 都调用已有的 `HybridRetriever`（BM25/FTS5 + Qwen3 向量 + RRF），多步骤并发执行：

```python
agentic_parallel_workers = 4
```

Agentic 多步骤阶段**关闭子步骤重排序**，只保留足够大的候选集合；所有步骤完成后再统一调用一次 Qwen3-Reranker。这样避免每条 Query 重复调用重排模型，也避免提前截断跨文档候选。

## 补充检索（受控回退）

当前补检是规则触发的受控回退，**不是**第二次 Planner LLM 推理循环。触发条件：

- 没有召回结果。
- 命中数量低于 `agentic_min_hits`（默认 4）。
- 对比问题只覆盖了一个目标文档。

补检用当前问题、聚焦 Query 和问题类型生成候选补检语句，继续受 `agentic_max_steps`（默认 4）限制。因此系统具备"一次规划、多步检索、条件补检"，还不是"LLM 阅读第一轮资料后自主制定第二轮计划"的完整多轮 Agent；后续增强方向是在补检前加一个结构化的 LLM 资料覆盖判断器。

## 最终资料包与回答

最终候选经过：按 chunk 去重 → Qwen3 全局重排 → 对比问题按文档平衡 → 生成带文档、页码、条款和图片上下文的 citations。

Answer LLM 只接收最终资料 JSON，不接收 Planner 内部过程；前端根据回答正文中的 `[n]` 编号关联第 `n` 条资料（回答没引用编号则不展示对应预览图）。

## 回退策略

```text
Planner LLM 不可用   -> 本地 QueryPlan 和规则检索步骤
Qwen3-Reranker 不可用 -> 硅基流动 fallback -> 本地词项排序
Embedding 不可用      -> 健康接口显示未就绪，稠密检索降级 BM25
```

每层都有明确降级路径，单点故障不阻断问答主链路。

## 关键配置

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `RAG_AGENTIC_RETRIEVAL_ENABLED` | `True` | Agentic 检索总开关 |
| `RAG_AGENTIC_PLANNER_LLM_ENABLED` | `True` | LLM 规划开关（关闭后走本地规则计划） |
| `RAG_AGENTIC_MAX_STEPS` | `4` | 单计划最大检索步骤数 |
| `RAG_AGENTIC_PARALLEL_WORKERS` | `4` | 步骤并发数 |
| `RAG_AGENTIC_MIN_HITS` | `4` | 低于此命中数触发补检 |

## 运维思考

| 维度 | 考量 |
| --- | --- |
| 时延 | Planner 增加一次 LLM 调用（约几百 ms），换来更精准的多步检索；`planner_ms` / `recall_ms` / `rerank_ms` / `first_token_ms` 分段写入 `storage/logs/chat-metrics.jsonl`，可直接定位慢点 |
| 高并发 | 4 并发检索步骤会同时打 embedding/rerank 服务，靠 vLLM continuous batching 消化；并发用户多时可下调 `agentic_parallel_workers` |
| token 成本 | Planner 输入只有文档目录 + Wiki metadata（数百 token），输出是短 JSON；输入输出均远小于回答 LLM，成本可控 |
| 可观测性 | 规划失败、JSON 不合法、回退到规则计划都有 warning 日志；AgentStep 记录 tool/query/reason，便于复盘"为什么检索了这些维度" |

## 维护注意

- Planner 提示词集中在 `prompt.py` 的 `PLANNER_SYSTEM_PROMPT`，修改工具列表时同步更新提示词和 `agent.py` 的 `ALLOWED_TOOLS`。
- 新增检索工具必须先过白名单校验设计，不允许模型自由调用任意函数。
- `search_wiki` 每计划只允许一次（`used_search_wiki` 标记），防止概念检索挤占正文检索预算。

## 相关测试

```powershell
cd E:\lmq\RAG_ZB
E:\lmq\RAG_ZB\.tools\uv\bin\uv.exe run --project backend pytest backend\tests\test_agent.py -q
```
