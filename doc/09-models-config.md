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

`ChatProvider` 会把检索证据整理成编号证据，并调用兼容 OpenAI 协议的 `/chat/completions` 接口。

请求格式：

```json
{
  "model": "jewelzufo/MiniCPM5-1B",
  "temperature": 0.1,
  "max_tokens": 1024,
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

## 向量化

`EmbeddingProvider` 有两种模式：

- 配置外部 Embedding 时，调用 `/embeddings`
- 未配置时，使用本地哈希向量，保证系统能离线跑通

正式效果测试建议配置中文 Embedding 模型，否则语义召回质量有限。

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

## Ollama 本地模型

本地 Ollama 可通过 OpenAI-compatible endpoint 接入：

```dotenv
RAG_OPENAI_BASE_URL=http://127.0.0.1:11434/v1
RAG_OPENAI_API_KEY=ollama-local
RAG_CHAT_MODEL=jewelzufo/MiniCPM5-1B
RAG_CHAT_MAX_TOKENS=1024
RAG_CHAT_THINK=false
```

`jewelzufo/MiniCPM5-1B` 可以用于功能联调，但由于模型规模较小，复杂规范归纳、新旧版本对比和证据筛选质量会明显受限。

## 维护注意

- 修改 `.env`、密钥、token、API 地址前必须先确认。
- 不要把真实密钥写入代码、README 或测试文件。
- 更换 Embedding 维度后要同步处理 Qdrant 集合和历史索引。
- 本地小模型可以做功能联调，但正式回答质量需要用更强的中文模型评测。
