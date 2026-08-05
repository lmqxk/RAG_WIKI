# 09 - 模型与配置

## 模块定位

模型与配置模块负责集中读取运行参数，并适配回答生成、向量化和重排服务。

主要文件：

- `backend/src/backend/config.py`
- `backend/src/backend/providers.py`
- `.env.example`

## 配置读取

所有业务配置统一使用 `RAG_` 前缀。后端会读取项目根目录 `.env`，也支持系统环境变量覆盖。

常用配置：

| 变量 | 说明 |
| --- | --- |
| `RAG_API_HOST` | API 监听地址 |
| `RAG_API_PORT` | API 端口 |
| `RAG_FRONTEND_ORIGIN` | 允许跨域的前端地址 |
| `RAG_DATA_DIR` | 数据目录 |
| `RAG_MAX_FILE_SIZE_MB` | PDF 文件大小上限 |
| `RAG_MAX_PDF_PAGES` | PDF 页数上限 |
| `RAG_OPENAI_BASE_URL` | OpenAI 兼容接口地址 |
| `RAG_OPENAI_API_KEY` | 模型接口密钥 |
| `RAG_CHAT_MODEL` | 回答模型 |
| `RAG_CHAT_MAX_TOKENS` | 回答最大输出 token |
| `RAG_CHAT_THINK` | 是否启用模型思考参数 |
| `RAG_EMBEDDING_MODEL` | 向量模型 |
| `RAG_EMBEDDING_DIMENSION` | 向量维度 |
| `RAG_RERANK_BASE_URL` | 重排接口地址 |
| `RAG_RERANK_MODEL` | 重排模型 |

## 回答生成

`ChatProvider` 会把检索资料整理成带编号的资料包，并调用兼容 OpenAI 协议的 `/chat/completions` 接口。

请求格式：

```json
{
  "model": "jewelzufo/MiniCPM5-1B",
  "temperature": 0.1,
  "max_tokens": 2048,
  "think": false,
  "messages": [
    {"role": "system", "content": "系统提示词"},
    {"role": "user", "content": "问题和证据"}
  ]
}
```

返回读取：

```json
{
  "choices": [
    {
      "message": {
        "content": "回答正文"
      }
    }
  ]
}
```

未配置回答模型时，系统使用摘录式回答：

- 事实类问题返回最相关原文依据
- 对比类问题按文档分组展示证据

## 对比资料包

普通问题传给 LLM 的资料是编号列表。对比类问题会使用按文档分组的资料包：

```json
{
  "topic": "对比 GB55037-2022 和 GB50016-2014 的防火间距差异",
  "mode": "cross_document_comparison",
  "documents": [
    {
      "document_id": "new",
      "document": "建筑防火通用规范",
      "standard_no": "GB55037-2022",
      "version": "2022",
      "items": [
        {
          "id": 1,
          "location": {"clause_no": "3.1.1", "pdf_page": 12},
          "type": "规范正文",
          "text": "..."
        }
      ]
    }
  ]
}
```

其中 `id` 仍然对应前端引用编号，例如 `[1]`、`[2]`。分组只是为了让模型更稳定地区分不同规范，避免把新旧文档混成一段回答。

模型提示词要求对比类回答优先包含：

- 结论：是否有直接可比资料。
- 对比要点：分别列出各文档适用对象、数值、条件和资料编号。
- 资料不足：说明不能直接判断变严或放宽的原因。

回答后端目前不再做正则清理，只保留首尾空白裁剪。内部图片路径会在传给 LLM 前替换为 `[图片见资料预览]`，避免模型直接输出存储路径。

## 向量化

`EmbeddingProvider` 有三种模式：

- 默认本地模式加载 `BAAI/bge-small-zh-v1.5`，在 API 进程内按需加载，GPU 优先、失败回退 CPU。
- 配置 `RAG_EMBEDDING_BACKEND=openai` 时，调用 `/embeddings`。
- 配置 `RAG_EMBEDDING_BACKEND=hash` 时，使用确定性哈希向量，仅用于离线调试。

本地模型目录默认是 `.models/modelscope/models/BAAI--bge-small-zh-v1.5`，可用以下命令下载：

```powershell
modelscope download --model BAAI/bge-small-zh-v1.5 `
  --local_dir E:\lmq\RAG_ZB\.models\modelscope\models\BAAI--bge-small-zh-v1.5
```

该模型输出 512 维向量。系统使用独立集合 `document_chunks_bge_small_zh_v1_5`，保留旧 384 维哈希集合，下载模型并重启后需要对已有文档执行 `reindex`。

### 启动预热

默认启动时会执行一次最小推理：

- API lifespan 初始化完成后预热 BGE。
- Rerank 服务健康检查通过后预热 Jina。
- 预热失败只记录警告，服务仍会启动，并在真实请求时再次尝试。

显存紧张或希望缩短启动时间时，可在 `config.py` 中关闭：

```python
embedding_warmup_on_start = False
local_rerank_warmup_on_start = False
```

Embedding 请求格式：

```json
{
  "model": "embedding-model",
  "input": ["文本1", "文本2"],
  "dimensions": 384
}
```

## 重排

`RerankProvider` 有两种模式：

- 配置外部重排接口时，调用外部服务
- 未配置时，使用词项重合、精确匹配和正文优先规则排序
- 外部重排接口不可用时，会自动回退到本地词项排序

Rerank 请求格式：

```json
{
  "model": "rerank-model",
  "query": "用户问题",
  "documents": ["候选文本1", "候选文本2"],
  "top_n": 8,
  "return_documents": false
}
```

### 本地 Jina Reranker

当前可使用 `jinaai/jina-reranker-v3.5` 作为本地重排模型。模型文件来自 ModelScope，默认目录：

```text
.models/modelscope/models/jinaai--jina-reranker-v3.5/snapshots/master
```

启用配置：

```dotenv
RAG_RERANK_BASE_URL=http://127.0.0.1:8011/rerank
RAG_RERANK_API_KEY=local
RAG_RERANK_MODEL=jina-reranker-v3.5
RAG_LOCAL_RERANK_DEVICE=auto
```

当 `RAG_RERANK_BASE_URL` 指向本地 `/rerank` 时，`start.cmd` 会自动启动本地 rerank 服务。该服务独立于主后端，第一次请求时懒加载模型，`auto` 设备策略会优先 GPU，失败后降级 CPU。

本地 rerank 服务采用单例模型工厂：

- 模型只在第一次 `/rerank` 请求时加载。
- 并发请求会通过加载锁避免重复加载模型。
- 推理阶段会串行进入模型，避免 Agentic 多步骤并行检索时同时打爆同一个本地 GPU 模型。
- 如果 GPU 推理失败，会尝试降级到 CPU。

Jina 模型加载时可能出现 `lm_head.weight` 未初始化提示。只要 `/rerank` 返回 `200 OK` 且排序结果正常，该提示不等于服务不可用；真正需要处理的是 `/rerank` 返回 `500`、连接失败或排序耗时异常。

## Agentic Planner LLM

Agentic 检索的规划层复用回答模型配置：

- `RAG_OPENAI_BASE_URL`
- `RAG_OPENAI_API_KEY`
- `RAG_CHAT_MODEL`

Planner LLM 只负责输出 JSON 检索计划，不直接生成最终回答。代码会用 Pydantic 校验 JSON，限制工具名和文档引用，避免模型随意调用不存在的工具。规划不可用时会回退到本地规则计划，所以系统仍能完成基础检索。

### Token 配置实验结论

当前实验表明，最终回答使用 `2048` token 时，跨文档对比和复杂资料归纳的完整性较好；降低到 `1024` token 容易在回答尚未完成时被截断。因此 `RAG_CHAT_MAX_TOKENS` 默认保持 `2048`。

Planner 的输出是短 JSON 检索计划，后续应使用独立的 Planner token 配额，不应为了压缩 Planner 而降低最终回答的 token 上限。

当前默认开启：

```text
agentic_retrieval_enabled = true
agentic_planner_llm_enabled = true
agentic_parallel_workers = 4
```

这部分默认值放在 `backend/src/backend/config.py`，`.env` 只建议放密钥、接口地址和临时覆盖项。

## 流式回答与日志

问答接口优先使用 `/api/chat/stream` 流式返回。后端会记录每次问答的性能日志：

```text
storage/logs/chat-metrics.jsonl
```

主要字段：

- `retrieval_ms`：检索和资料组织耗时。
- `first_token_ms`：首个回答 token 返回时间。
- `total_ms`：完整请求耗时。
- `answer_chars`：最终回答字符数。
- `citations`：返回资料数量。
- `query_type`：问题类型，例如 `comparison`。

## Ollama 本地模型

本地 Ollama 可通过 OpenAI-compatible endpoint 接入：

```dotenv
RAG_OPENAI_BASE_URL=http://127.0.0.1:11434/v1
RAG_OPENAI_API_KEY=ollama-local
RAG_CHAT_MODEL=jewelzufo/MiniCPM5-1B
RAG_CHAT_MAX_TOKENS=2048
RAG_CHAT_THINK=false
```

`jewelzufo/MiniCPM5-1B` 可以用于功能联调，但由于模型规模较小，复杂规范归纳、新旧版本对比和证据筛选质量会明显受限。

## 维护注意

- 修改 `.env`、密钥、token、API 地址前必须先确认。
- 不要把真实密钥写入代码、README 或测试文件。
- 更换 Embedding 模型或维度后，必须使用新的 Qdrant 集合并对历史文档重新执行 `reindex`。
- 本地小模型可以做功能联调，但正式回答质量需要用更强的中文模型评测。
