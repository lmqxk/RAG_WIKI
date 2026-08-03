# 02 - Web 工作台

## 模块定位

Web 工作台是用户使用系统的主入口，负责知识库管理、文件上传、文档选择、自然语言提问、回答展示和引用证据查看。

主要文件：

- `frontend/app/RagDashboard.tsx`
- `frontend/app/page.tsx`
- `frontend/app/layout.tsx`
- `frontend/app/globals.css`
- `frontend/components/ui/*`

## 技术实现

| 项 | 实现 |
| --- | --- |
| 框架 | Next.js App Router |
| UI | shadcn/ui + Tailwind CSS |
| 图标 | lucide-react |
| 状态管理 | React `useState`、`useEffect`、`useMemo` |
| API 请求 | 浏览器 `fetch` |
| 环境变量 | `NEXT_PUBLIC_API_BASE_URL` |
| PDF 跳转 | `<a href="/api/documents/{id}/file#page={page}">` |

## 页面结构

| 区域 | 职责 |
| --- | --- |
| 左侧导航 | 在“智能问答”和“知识库”之间切换 |
| 顶部栏 | 展示当前视图、服务状态和帮助入口 |
| 智能问答 | 展示对话、输入问题、触发问答 |
| 本轮检索范围 | 选择本轮参与回答的规范文档 |
| 知识库 | 上传 PDF、搜索文档、查看解析状态 |
| 引用抽屉 | 展示证据片段、条款号、页码和 PDF 跳转 |

## 前端状态

| 状态 | 含义 |
| --- | --- |
| `documents` | 当前知识库文档列表 |
| `selectedIds` | 本轮问答选中的文档 ID |
| `jobs` | 后台解析任务状态 |
| `health` | 后端健康状态和模型配置状态 |
| `messages` | 当前对话消息 |
| `activeResult` | 当前选中的回答结果，用于展示引用 |

## API 调用

前端通过 `NEXT_PUBLIC_API_BASE_URL` 指向后端，默认值为 `http://127.0.0.1:8000`。

| 操作 | API |
| --- | --- |
| 健康检查 | `GET /api/health` |
| 文档列表 | `GET /api/documents` |
| 上传 PDF | `POST /api/documents` |
| 查询任务 | `GET /api/jobs/{job_id}` |
| 提问 | `POST /api/chat` |
| 打开 PDF | `GET /api/documents/{document_id}/file#page={page}` |

## 交互细节

- 上传后会创建后台任务，前端每 1.5 秒轮询任务状态。
- 如果有可检索文档但用户没有手动选择，系统默认选择全部可用文档。
- 问答完成后，回答正文显示在对话区，引用证据通过按钮打开右侧抽屉。
- 文档未完成解析时不能加入问答范围。

## 维护注意

- 页面文案应保持产品化，不要直接展示内部实现词。
- 技术实现细节写到 `doc`，不要写进面向用户的页面文本。
- 引用入口可以出现在回答消息里，但不要在右上角堆重复按钮。
- 规范系统属于工作台类产品，界面应保持紧凑、清晰、可扫描。
- 修改 UI 后建议运行前端构建，并用浏览器检查桌面和窄屏布局。

## 前端验证

```powershell
cd E:\lmq\RAG_ZB
$node = "C:\Users\PC\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe"
& $node ".tools\npm-cli\package\bin\npm-cli.js" run build --prefix frontend
```

如果要做真实浏览器检查，使用 Playwright 打开 `http://localhost:3000`，重点检查：

- 左侧导航和顶部栏是否重复。
- 问答区长文本是否溢出。
- 知识库卡片在 1366、1920 和移动宽度下是否可读。
- 引用抽屉是否能打开 PDF 对应页。
