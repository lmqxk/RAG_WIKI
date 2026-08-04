# 01 - 项目总览

## 模块定位

本项目是面向规范、标准、制度、法规类 PDF 的知识库问答系统。当前产品形态优先支持“知识库模式”：用户上传 PDF，系统解析、入库、建立索引后，在 Web 工作台中按已选文档范围提问，并返回带原文引用的回答。

## 总体链路

```text
PDF 上传
  -> API 接收并保存文件
  -> 创建文档记录和后台任务
  -> PDF 内容解析
  -> 章节、条款、页码结构化
  -> 文本块切分
  -> SQLite 全文索引和 Qdrant 本地向量索引写入
  -> 用户提问
  -> 多路检索与排序
  -> 模型生成或证据摘录
  -> 回答和引用返回 Web 端
```

## 技术栈

| 层级 | 技术 |
| --- | --- |
| Web | Next.js、React、TypeScript、Tailwind CSS、shadcn/ui、lucide-react |
| API | FastAPI、Pydantic、Uvicorn |
| PDF 解析 | OpenDataLab PDF-Extract-Kit、MinerU VLM、PyMuPDF、RapidOCR |
| 数据库存储 | SQLite、WAL、FTS5 |
| 向量索引 | Qdrant Local |
| RAG 检索 | QueryPlan、BM25、Vector Search、Reciprocal Rank Fusion、Reranker |
| LLM 调用 | Ollama、本地模型、OpenAI-compatible Chat Completions |
| Python 环境 | uv、项目级 `.venv` |
| 前端依赖 | npm、项目级 `node_modules` |

## Document Intelligence + RAG Agent 设计

系统内部可以按两层理解：

- Document Intelligence：默认通过 OpenDataLab PDF-Extract-Kit 做 PDF document understanding，包括文本层识别、OCR、版面块提取、章节条款识别、表格处理、图片位置、页码映射和结构化落盘。
- RAG Agent：负责 Query Understanding、Hybrid Retrieval、Rerank、证据组织、LLM 生成和引用回链。

这两个概念不建议出现在最终用户界面，但必须出现在工程文档中，方便后续拆模块、扩展模型和定位问题。

## 主要目录

| 路径 | 作用 |
| --- | --- |
| `frontend/app/RagDashboard.tsx` | Web 工作台主界面 |
| `backend/src/backend/main.py` | FastAPI 入口和路由 |
| `backend/src/backend/parser.py` | PDF 解析 |
| `backend/src/backend/ingestion.py` | 文档入库任务编排 |
| `backend/src/backend/chunking.py` | 文本块切分 |
| `backend/src/backend/retrieval.py` | 检索流程编排 |
| `backend/src/backend/service.py` | 上传、问答和引用整理 |
| `backend/src/backend/repository.py` | SQLite 数据访问 |
| `backend/src/backend/vector_index.py` | Qdrant 本地索引 |
| `backend/src/backend/providers.py` | 模型、向量化、重排适配 |

## 当前边界

- 当前适合少量规范文件的单机知识库，不是多租户平台。
- 单个 PDF 默认上限由 `RAG_MAX_FILE_SIZE_MB` 和 `RAG_MAX_PDF_PAGES` 控制。
- 未配置外部模型时，系统仍可返回检索证据，但综合归纳能力有限。
- 对比类问题使用轻量 Agentic 编排：先规划目标文档，再分文档检索并按文档组织资料包。
- Qdrant 当前使用本地嵌入式存储；规模扩大后建议迁移到独立 Qdrant 服务。

## 维护原则

- 用户界面只展示产品语义，不展示内部技术链路；工程文档必须写清楚技术链路。
- 回答必须附引用，引用要能回到 PDF 原页。
- 条款号、页码、正文/说明区分是核心资产，解析和切分阶段不能随意丢。
- 后端配置统一走 `RAG_` 环境变量和项目根 `.env`，不要把密钥写进代码。
