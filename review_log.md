# 代码审计记录

> 记录每次 git 提交周期的修改过程。fix 用审计模板，feature 用需求/方案模板。
> 提交前必须先迭代本文件（规范见 `AGENTS.md`）。

## 2026-09-03 fix: 引用点击 404，无法跳转源 PDF

- 表层问题：对话中的引用图标点击无响应或 404，前端声称引用了 6 份资料但均无法打开。
- 根因：文档入库时在 `storage/uploads` 下保留了原始子目录层级，而 `main.py` 的路径解析只查平铺路径；另外 Docker 容器内数据库记录的宿主机绝对路径不可用。
- 修复：`main.py::resolve_storage_path()` 增加多级回退：原始绝对路径 → 挂载目录+文件名 → 挂载目录+子目录相对层级（路径分隔符先归一化）。
- 同类回扫：全量 6 份已入库 PDF 的引用资源接口逐一验证。
- 验证：6/6 文档的 PDF 页面接口返回 200，前端引用卡片可跳转原页。

## 2026-09-03 fix: 引用列表机械凑数

- 表层问题：每个回答固定返回 6 条引用，与正文实际标注的 `[n]` 不一致，引用列表含未被使用的资料。
- 根因：`answer_max_citations` 上限被当作固定数量返回，未与正文引用编号联动。
- 修复：`service.py` 增加 `_used_citations` 过滤，只返回正文中实际标注 `[n]` 的资料。
- 同类回扫：简单事实题、系统总结题、跨文档对比题三类问题分别验证引用数与正文一致性。
- 验证：简单事实问题返回 2 条引用、系统类问题返回 4 条，均与正文标注一致。

## 2026-09-03 fix: 前端向用户暴露系统状态

- 表层问题：页面左下角系统状态面板（embedding/检索状态等）对最终用户可见。
- 根因：开发调试信息未区分使用环境。
- 修复：前端隐藏系统状态展示，仅保留必要的错误提示。
- 验证：页面不再显示系统状态面板。

## 2026-09-03 fix: 日志目录功能重叠

- 表层问题：根目录 `logs/` 与 `storage/logs/` 两个日志目录并存。
- 根因：`main.py` 的文件日志写 `PROJECT_ROOT/logs`，其余模块写 `settings.data_dir/logs`。
- 修复：`configure_file_logging()` 统一写 `storage/logs/backend.log`，并升级为 `RotatingFileHandler`（10MB × 5 备份）；删除根目录 `logs/`。
- 同类回扫：确认 `.gitignore` 覆盖 `storage/`，运行日志只出现在 `storage/logs/`。
- 验证：重启后端，日志仅写入 `storage/logs/backend.log`。

## 2026-09-03 feature: Qwen3 Embedding/Reranker vLLM 容器化部署

- 需求：语义检索需要真实语义向量；此前 hash 向量降级模式语义检索失效，本地 transformers 加载无并发优化；要求可拆卸、可在任意电脑部署。
- 方案：vLLM 容器 + Qwen3-Embedding-0.6B（1024 维）+ Qwen3-Reranker-0.6B，Docker Compose `--profile gpu` 控制启停；备选 TEI/Ollama/Xinference 因支持面与 Windows GPU 兼容性排除。
- 实现：`docker-compose.yml`（embedding 8010 / reranker 8011，`--gpu-memory-utilization 0.2`、`--max-model-len 4096`、reranker `--hf-overrides` 分类头）、`scripts/deploy.ps1` 自动检测 GPU、`scripts/rebuild_vectors.py` 复用 chunk ID 重建向量、`providers.py` Qwen3 查询 Instruct 前缀与 rerank 模板、新集合 `document_chunks_qwen3_embedding_v1`。
- 同类扩展：backend/frontend Dockerfile 与健康检查依赖链；无 GPU 机器自动降级 BM25。
- 验证：Qdrant 重建 2170 chunks；五容器（embedding/reranker/backend/frontend/qdrant）健康检查全部通过，端到端问答语义检索生效。

## 2026-09-03 feature: Wiki 全库重建与整文档概念抽取

- 需求：Wiki 概念抽取此前只处理 8-chunk 采样，跨文档概念关联残缺；Wiki 重建曾因重新生成 chunk UUID 导致锚点断裂。
- 方案：概念抽取覆盖整文档（分批受 `wiki_analysis_max_batches` 控制）；重建复用数据库既有 chunk ID。
- 实现：`wiki.py` 整文档分析管线、`scripts/rebuild_wiki.py` 全库重建、`agent.py` 新增 `search_wiki` 工具（按概念名取概念页关联 chunk 集合，每计划限一次）、related 图四信号计分。
- 同类扩展：`main.py` 装配 WikiManager 到 `app.state`；LLM 失败回退确定性摘要。
- 验证：`backend/tests/test_wiki.py`、`test_agent.py` 通过；全库重建后锚点无 404。

## 2026-09-03 feature: 文本切分质量改进

- 需求：规范 PDF 前置页（目录/公告/版权页）和扫描水印污染检索索引；条款缺少父子层级关联。
- 方案：前置页识别进入 `front_matter` 区并跳过（保留表格/图片正文信号与空索引回退）；水印 URL 过滤；按条款号前缀解析 `parent_id`。
- 实现：`chunking.py` 增加 `FRONT_MATTER_RE`、`WATERMARK_RE`、front_matter 状态机、`parent_id` 解析、超长句硬切分。
- 同类扩展：`scripts/show_chunks.py` 支持 `--fresh` 从 normalized.json 预览新切分结果。
- 验证：`backend/tests/test_chunking_tables.py` 通过；reindex 后抽查 chunk 无目录页/水印文本。

## 2026-09-03 feature: 前端 Wiki 工作区

- 需求：Wiki 概念图谱需要可视化浏览入口。
- 实现：`frontend/app/RagDashboard.tsx` 重构（替代 `RagWorkbench.tsx`），新增 `components/wiki-workspace.tsx`（概念图谱、文档页、概念页导航），`markdown-message.tsx` 引用渲染改进。
- 验证：`npm run build` 通过，页面可正常切换智能问答/知识库/Wiki 工作区。

## 2026-09-03 feature: 文档体系重组与工程规范

- 需求：doc/ 与 docs/ 双目录并存且内容分层混乱；缺少面向算法/开发岗位的技术细节（选型对比、运维思考）。
- 方案：合并为 `docs/`，中文分类文件夹（01-系统总览 ~ 07-开发环境），每篇文档必含"选型对比"与"运维思考"表格。
- 实现：13 篇模块技术文档 + `docs/README.md` 总索引；新增《Docker与vLLM技术细节》（含 vLLM 参数、模型选型、显存/性能账）；根目录 `review_log.md`（本文件）与 `AGENTS.md` agent 规则；README.md 同步更新模型/管线描述与文档链接。
- 同类扩展：删除旧 `doc/` 12 篇与已吸收的 `docs/docker-deploy.md`。
- 验证：文档链接抽查可跳转；`docs/README.md` 与实际目录一致。

## 2026-09-03 feature: 评测集与引用验证脚本

- 需求：检索与回答质量需要可复现的评测输入。
- 实现：`eval/` 下新增多批高级评测问题集与 GB55037 vs GB50016 对比评测数据；`scripts/_verify_citations.py` 用于批量核验引用可达性。
- 验证：评测数据 JSONL 格式校验通过。
