# Agent 工程规范（RAG_ZB）

> 本文件是项目级 agent 编程规范，位于项目根目录 `AGENTS.md`。
> 每次会话开始处理本项目的编码任务前必须遵循本规范。

## 1. 文档维护规范

技术文档统一放在 `docs/`，按中文分类文件夹组织：

```text
docs/
├─ README.md                # 总索引（登记所有模块文档路径，新文档必须同步登记）
├─ 01-系统总览/             # 系统架构、Web工作台、API接口
├─ 02-文档处理/             # 文档解析、入库流水线、文本切分
├─ 03-检索与排序/           # 混合检索、存储与索引
├─ 04-模型服务/             # Docker与vLLM技术细节、模型与配置
├─ 05-Agentic检索/          # AgenticRAG
├─ 06-Wiki知识层/           # Wiki派生知识层
└─ 07-开发环境/             # 环境配置与恢复
```

规则：

1. 新增模块文档：放入对应中文分类文件夹（新模块先建分类文件夹），并在 `docs/README.md` 登记路径。
2. 每篇文档必须包含两个表格章节：**选型对比**（方案/优劣/结论）与**运维思考**（高并发/时间/显存等维度）。
3. 代码行为变更后，同步更新对应模块文档，改动与实际代码保持一致。
4. 不创建 docs 目录之外的零散技术文档；临时笔记不要提交。

## 2. review_log.md 迭代规范

`review_log.md` 位于项目根目录，记录每个 git 提交周期的修改过程。

**每次 git 提交前必须先迭代本文件**，按模板新增条目（新条目插在文件开头、紧跟文件说明之后，全文件按时间倒序，最新在最上）：

fix 模板：

```markdown
## YYYY-MM-DD fix: 一句话标题
- 表层问题：
- 根因：
- 修复：
- 同类回扫：
- 验证：
```

feature 模板：

```markdown
## YYYY-MM-DD feature: 一句话标题
- 需求：
- 方案：
- 实现：
- 同类扩展：
- 验证：
```

要求：条目必须写清根因/方案与验证结果，不接受"改好了"式记录。

## 3. 工程硬约束

- Wiki 重建必须复用数据库既有 chunk ID，禁止重新 `build_chunks()` 生成新 UUID（锚点会断）。
- LLM 概念抽取处理整文档分批，不做 chunk 采样。
- 测试环境隔离真实 API（`_env_file=None`）。
- 文档存储路径保留子目录层级（`resolve_storage_path` 的回退逻辑）。
- 引用展示只返回正文实际标注 `[n]` 的资料。
- Embedding 用 Qwen3-Embedding-0.6B（1024 维，query 侧加 Instruct 前缀），Rerank 用 Qwen3-Reranker-0.6B（`<Instruct>/<Query>/<Document>` 模板）。
- 模型服务必须容器化（vLLM，`--profile gpu` 可拆卸）。
- 日志统一写 `storage/logs/`，禁止新建根目录 `logs/`。
- 容器运行时不从宿主机直接打开 `storage/` 下的 SQLite（会触发 Docker Desktop 文件共享锁，导致容器内 `unable to open database file`；需查库进容器操作，宿主机侧只做文件复制）。
- 修改 `.env`、密钥、数据库结构、删除文件前先向用户确认。

## 4. 环境备忘

- Python 环境：`backend/.venv`（uv 管理，无 pip）；测试命令用 `.\backend\.venv\Scripts\python.exe -m pytest backend\tests -q`。
- 本地开发 `start.cmd`；Docker 部署 `powershell scripts/deploy.ps1`；两者不可同时运行。
