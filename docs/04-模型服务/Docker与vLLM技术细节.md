# Docker 与 vLLM 技术细节

主要文件：

- `docker-compose.yml`
- `backend/Dockerfile`、`frontend/Dockerfile`
- `scripts/deploy.ps1`
- `backend/src/backend/providers.py`（Qwen3 查询模板）

## 模块定位

本文说明 Embedding / Reranker 模型服务的容器化部署与 vLLM 推理引擎的技术细节，包括选型依据、参数含义、显存分配和并发模型。适合作为技术汇报中"推理服务架构"部分的素材。

## 容器编排总览

```text
浏览器 ──> frontend (:3000) ──> backend (:8008) ──> qdrant (:6333)
                                ├──> Ollama（宿主机 host.docker.internal:11434，LLM 问答）
                                ├──> PaddleVL（宿主机 host.docker.internal:8080，PDF 解析）
                                ├──> embedding（gpu profile，8010，Qwen3-Embedding-0.6B）
                                └──> reranker（gpu profile，8011，Qwen3-Reranker-0.6B）
```

| 容器 | 镜像 | 端口 | Profile | 职责 |
| --- | --- | --- | --- | --- |
| `rag-zb-qdrant` | `qdrant/qdrant:v1.16.2` | 6333 | 基础 | 向量索引存储 |
| `rag-zb-backend` | 自建（`backend/Dockerfile`） | 8008 | 基础 | FastAPI 后端 |
| `rag-zb-frontend` | 自建（`frontend/Dockerfile`） | 3000 | 基础 | Next.js 前端 |
| `rag-zb-embedding` | `vllm/vllm-openai:latest` | 8010→8000 | `gpu` | Qwen3-Embedding-0.6B 语义向量 |
| `rag-zb-reranker` | `vllm/vllm-openai:latest` | 8011→8000 | `gpu` | Qwen3-Reranker-0.6B 精排 |

关键设计：

- **GPU 服务可拆卸**：两个模型服务标记 `profiles: ["gpu"]`，默认 `docker compose up -d` 只启动基础三件套，稠密检索自动降级 BM25；有 NVIDIA GPU 时 `--profile gpu` 附带启动。
- **数据与计算分离**：全部业务数据挂载宿主机 `./storage/`，容器无状态，重建容器不丢数据；模型缓存放 `rag-zb-models` 卷，避免重复下载。
- **端口只绑 127.0.0.1**：8010/8011 仅宿主机可访问（供 `rebuild_vectors.py` 等脚本直连），不暴露局域网。

## vLLM 是什么

vLLM 是高吞吐 LLM 推理引擎（UC Berkeley 开源），核心是两项技术：

1. **PagedAttention**：把 KV cache 切成固定页管理（类比操作系统虚拟内存），消除传统推理中按 max_len 预留显存造成的碎片浪费，显存利用率大幅提升。
2. **Continuous Batching（持续批处理）**：请求到齐即入队、完成即出队，不需要等整批一起结束。对 embedding 这种短序列高频请求，吞吐提升数倍。

它同时暴露 **OpenAI 兼容 HTTP API**（`/v1/embeddings`、`/v1/chat/completions`、score/rerank 端点），后端只需要一个 HTTP 客户端就能接入，和调用商业 API 代码形态一致。

## 推理引擎选型对比

| 方案 | 优势 | 劣势 | 结论 |
| --- | --- | --- | --- |
| **vLLM**（采用） | PagedAttention + continuous batching 吞吐高；OpenAI 兼容协议；支持 embedding 与 sequence-classification（reranker）；社区活跃 | 首次加载模型稍慢；镜像较大（约 8GB） | **采用**：单引擎同时跑 embedding 和 reranker 两个容器，运维心智统一 |
| TEI（Text Embeddings Inference） | HuggingFace 官方，embedding/rerank 专用，轻量 | 功能面窄；Windows GPU 环境兼容性当时不佳 | 备选 |
| Ollama | 桌面级易用 | 当时对 Qwen3-Embedding/Reranker 的加载与 score API 支持不全 | 用于 LLM（deepseek-r1:1.5b），不用于向量服务 |
| Xinference | 全家桶（LLM/embedding/rerank 都管） | 服务本身偏重、层级多，与"可拆卸"目标不符 | 不采用 |
| 自写 FastAPI + transformers | 完全可控 | 无 continuous batching，无页化显存管理，并发吞吐差一个量级 | 仅旧版本本地模式保留（已废弃为主路径） |

## vLLM 关键启动参数详解

### 通用参数（两个容器共用）

| 参数 | 值 | 含义与理由 |
| --- | --- | --- |
| `Qwen/Qwen3-Embedding-0.6B` | 位置参数 | HuggingFace 模型 ID；配合环境变量 `VLLM_USE_MODELSCOPE=1` 改从 ModelScope（国内源）下载 |
| `--served-model-name` | `qwen3-embedding` / `qwen3-reranker` | API 中的 model 字段名，与 `.env` 的 `RAG_EMBEDDING_MODEL` / `RAG_RERANK_MODEL` 对应 |
| `--max-model-len` | `4096` | 单请求最大 token 数。切分模块把单块控制在约 1400 字符（约 1k token），4096 留足余量；调小可省 KV cache 显存 |
| `--gpu-memory-utilization` | `0.2` | vLLM 允许占用的 GPU 显存比例。两个模型容器各 20%，合计 40%，剩余留给 PaddleVL 解析服务等 |
| `--host 0.0.0.0 --port 8000` | 容器内监听 | 端口映射到宿主机 8010/8011 |
| `VLLM_USE_MODELSCOPE=1` | 环境变量 | 模型走 ModelScope 镜像下载，国内网络下载数十分钟 vs HF 超时 |

### Reranker 专属：`--hf-overrides`

```json
{
  "architectures": ["Qwen3ForSequenceClassification"],
  "classifier_from_token": ["no", "yes"],
  "is_original_qwen3_reranker": true
}
```

Qwen3-Reranker 原始权重是生成式模型（输出 "yes"/"no" 文本），vLLM 通过覆盖 config：

- 把模型加载为 **sequence classification** 架构（分类头），而不是文本生成
- `classifier_from_token: ["no","yes"]` 告诉 vLLM 从 "no"/"yes" 两个 token 的 logits 取值，softmax 后取 "yes" 的概率作为 0~1 相关性分数——**单次前向直接得到分数，无需生成文本再解析**，这也是 reranker 能稳定低延迟的原因
- `is_original_qwen3_reranker` 保持与官方推荐格式一致

### 为什么在代码侧拼模板而不是让 vLLM 拼

`providers.py` 中 Qwen3-Reranker 请求格式（`_qwen3_rerank_texts`）：

```text
<|im_start|>system
Judge whether the Document meets the requirements based on the Query
and the Instruct provided. Note that the answer can only be "yes" or "no".
<|im_end|>
<|im_start|>user
<Instruct>: {任务指令}
<Query>: {用户问题}
<Document>: {候选条款}
<|im_end|>
<|im_start|>assistant
@｜结束符

```

Embedding 查询侧（`_query_instruction`）：

```text
Instruct: Given a web search query, retrieve relevant passages that answer the query
Query: {用户问题}
```

- Qwen3-Embedding 是**指令感知（instruct-aware）非对称模型**：query 侧加 Instruct 前缀、document 侧不加，是官方手册规定的用法，不加会导致检索精度明显下降。
- 模板放在代码侧而非 vLLM 参数里，便于单元测试（`test_providers.py` 直接断言模板格式），也避免升级 vLLM 时模板行为变化。

## 模型参数与选型对比

### Embedding 模型对比

| 模型 | 参数量 | 维度 | 中文 | 显存（bf16） | 结论 |
| --- | --- | --- | --- | --- | --- |
| **Qwen3-Embedding-0.6B**（采用） | 0.6B | 1024（MRL 可降到 32~1024） | 强（C-MTEB 中文领先） | 约 1.2GB | 中文效果好、同家族 reranker 可搭配、0.6B 显存友好 |
| BAAI/bge-small-zh-v1.5 | 0.03B | 512 | 好 | 约 0.1GB | 旧方案，效果与 Qwen3 有差距；曾作为本地默认 |
| BAAI/bge-large-zh-v1.5 | 0.3B | 1024 | 好 | 约 1GB | 无 instruct 感知，多任务指令场景弱于 Qwen3 |
| 商业 API（Jina 等） | - | - | 强 | 0 | 依赖外网与配额，与"任意电脑可部署"目标冲突，仅作 fallback |

Qwen3-Embedding 关键参数：

| 项 | 值 | 说明 |
| --- | --- | --- |
| 向量维度 | 1024 | 支持 MRL（Matryoshka）截断到 32~1024 维，本项目用全维度 1024 |
| 上下文长度 | 32k（本项目限制为 4096） | 服务统一 4096，chunk 最大约 1400 字符 |
| 池化方式 | last-token pooling + 无训练激活 | 官方特性：不加额外池化层即可获得强表征 |
| 训练数据 | 26+ 语种、多种任务指令 | 中英混合规范文本友好 |

### Reranker 模型对比

| 模型 | 类型 | 显存 | 结论 |
| --- | --- | --- | --- |
| **Qwen3-Reranker-0.6B**（采用） | 交叉编码（yes/no 概率） | 约 1.2GB | 与 Embedding 同源架构，vLLM 同镜像部署，运维统一 |
| jina-reranker-v3.5 | 交叉编码 | 约 3GB+ | 旧本地方案：效果可用但更重，需独立 FastAPI 服务进程 |
| BAAI/bge-reranker-v2-m3 | 交叉编码 | 约 2GB | 作为云端 fallback（硅基流动）保留配置位 |
| 无 reranker | 词项重合度排序 | 0 | 兜底：外部服务全挂时仍可排序，不阻断问答 |

### 调用参数

| 项 | 值 | 说明 |
| --- | --- | --- |
| `RAG_EMBEDDING_DIMENSION` | 1024 | Qdrant 集合维度，校验返回向量一致 |
| `RAG_EMBEDDING_BATCH_SIZE` | 16 | providers 单次请求文本条数，配合 vLLM batching |
| Qdrant upsert 批 | 32 | `vector_index.py` 每批 embed+upsert |
| rerank `top_n` | 8 | 重排后保留最终证据数 |

## 显存与性能账

以单张 8GB 消费级卡（如 RTX 4060）为例：

| 项 | 占用 | 说明 |
| --- | --- | --- |
| Qwen3-Embedding-0.6B 权重（bf16） | 约 1.2GB | `--gpu-memory-utilization 0.2` 上限约 1.6GB，权重 + KV cache 页池 |
| Qwen3-Reranker-0.6B 权重（bf16） | 约 1.2GB | 同上 |
| 合计模型服务 | 约 2.4~3.2GB | 两容器各 20% 预算，剩余 60% 可留给 PaddleVL 或未来 LLM 本地化 |
| 全库向量数据（Qdrant，内存/磁盘） | 约 10MB | 2170 chunks × 1024 维 × 4B |

时间账：

| 环节 | 耗时 | 说明 |
| --- | --- | --- |
| 首次模型下载 | 各约 1.2GB，数分钟 | ModelScope 源，缓存进 `rag-zb-models` 卷，只发生一次 |
| 容器冷启动 | 约 30 秒 | 加载权重到 GPU；healthcheck `start_period` 放宽到 600s 兜底首次下载 |
| 单条 embedding | 20~50ms | vLLM 批处理下摊薄；全库 2170 chunks 重建约 1 分钟 |
| 单次 rerank（20 候选） | 100~300ms | 单次前向出分，与候选文本长度线性相关 |
| 并发增长行为 | 亚线性 | continuous batching：并发 10 路时单路时延增幅远小于 10 倍 |

## 后端接入配置

`docker-compose.yml` 的 `environment` 覆盖优先级高于 `.env`（容器内网络与宿主机不同）：

| 变量 | 容器内取值 | 原因 |
| --- | --- | --- |
| `RAG_EMBEDDING_BASE_URL` | `http://embedding:8000/v1` | 走 compose 内部服务名（gpu profile） |
| `RAG_RERANK_BASE_URL` | `http://reranker:8000/v1/rerank` | 同上 |
| `RAG_QDRANT_URL` | `http://qdrant:6333` | 内部服务名 |
| `RAG_OPENAI_BASE_URL` | `http://host.docker.internal:11434/v1/` | Ollama 在宿主机 |
| `RAG_PADDLEVL_API_URL` | `http://host.docker.internal:8080` | PaddleVL 在宿主机 |
| `RAG_API_HOST` | `0.0.0.0` | 容器内必须对外监听 |

宿主机脚本（`rebuild_vectors.py`）直连模型走 `http://127.0.0.1:8010` / `:8011`（对应 `.env`）。

## 部署与运维命令

```powershell
powershell scripts/deploy.ps1            # 一键：GPU 检测 + 构建 + 启动 + 健康检查
powershell scripts/deploy.ps1 -Rebuild    # 代码改动后重建镜像并启动
powershell scripts/deploy.ps1 -Down       # 停止全部服务

docker compose --profile gpu up -d        # 手动附带启动模型服务
docker compose logs -f embedding          # 看模型服务日志
docker compose --profile gpu ps           # 五容器健康状态
```

`deploy.ps1` 自动完成：移除旧独立 Qdrant 容器（数据保留）、端口占用检查、`nvidia-smi` 检测 GPU 决定是否带 `--profile gpu`、等待健康检查后打印访问地址。

### 健康检查设计

所有容器都有 healthcheck，backend/frontend 的 `depends_on: condition: service_healthy` 保证启动顺序：

```yaml
# 模型容器：内部 HTTP 探活，start_period 600s 容忍首次下载
test: ["CMD", "python3", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).status < 500 else 1)"]
```

### 常见问题

- **首次启动很慢**：正常，在下载模型（各约 1.2GB），完成后缓存于 `rag-zb-models` 卷。
- **换 Embedding 模型后检索错乱**：新旧向量量纲不一致，运行 `backend\.venv\Scripts\python.exe scripts\rebuild_vectors.py` 重建（chunk ID 不变，约 1 分钟）。
- **无 GPU 机器**：`docker compose up -d` 基础三件套照常运行，稠密检索降级 BM25；或把 `RAG_EMBEDDING_BASE_URL` / `RAG_RERANK_BASE_URL` 指向局域网 GPU 服务器（key 非空才启用外部服务）。

## 运维思考

| 维度 | 考量 |
| --- | --- |
| 高并发 | continuous batching 是并发第一道防线；Qwen3-0.6B 级小模型单卡吞吐高，10 并发问答的 embedding/rerank 延迟增幅可控；再往上可提 `--gpu-memory-utilization` 扩大 KV 页池或复制容器 |
| 显存 | 两模型各锁 20% 显存预算，与解析服务（PaddleVL）、LLM 共卡不冲突；调大前先确认同卡其他服务的峰值占用 |
| 时间 | 模型缓存卷 + 常驻容器（`restart: unless-stopped`）把"下载/加载"从请求路径中移除，运行时只有推理时间 |
| 可移植 | 镜像 + 卷 + `storage/` 三件套可整体迁移到任何装 Docker 的机器；GPU profile 缺失时系统仍可用（降级运行），符合"任意电脑可部署"目标 |
| 故障域 | 模型容器挂掉不影响 BM25 词法检索和问答主链路，`/api/health` 可观测 embedding/rerank 就绪状态 |
