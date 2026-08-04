# 06 - 文本切分

## 模块定位

文本切分模块负责把解析后的页面文本块转换成可检索、可引用、可排序的知识片段。切分质量直接影响条款定位、综合回答和引用准确性。

主要文件：

- `backend/src/backend/chunking.py`
- `backend/src/backend/domain.py`

## 技术实现

切分模块是纯 Python 规则系统，主要依赖正则表达式和解析阶段保留的页面 block 顺序。

关键正则：

```python
CLAUSE_RE = r"^\s*(\d+(?:\.\d+){2,5})\s*(.+)$"
SECTION_RE = r"^\s*(\d+(?:\.\d+)?)\s+([\u3400-\u9fff].{0,80})$"
CHINESE_HEADING_RE = r"^\s*第[一二三四五六七八九十百]+[编篇章节]\s*.*$"
```

## 输入输出

输入：

- `document_id`
- `PageBlock` 列表

输出：

- `Chunk` 列表

## 识别规则

| 规则 | 说明 |
| --- | --- |
| `CLAUSE_RE` | 识别 `3.1.2`、`4.1.4` 这类条款号 |
| `SECTION_RE` | 识别一级、二级数字章节标题 |
| `CHINESE_HEADING_RE` | 识别“第X章”“第X节”等中文标题 |
| `条文说明` | 进入说明区，后续片段标记为 commentary |
| 表格类型 | 单独切片，并保留表格来源类型 |

## Chunk 字段

| 字段 | 含义 |
| --- | --- |
| `id` | 文本块唯一 ID |
| `document_id` | 所属文档 |
| `ordinal` | 文档内顺序 |
| `chapter_path` | 章节路径 |
| `clause_no` | 条款号 |
| `text` | 文本内容 |
| `page_start` / `page_end` | PDF 页码范围 |
| `printed_page` | 纸面页码 |
| `source_type` | 正文、说明、表格等来源 |
| `parent_id` | 父级条款 ID |

`PageBlock.images` 不直接写入 `chunks` 表的独立字段，但表格文本里仍会包含图片提示文本，例如：

```text
图片文件：images/f078.jpg
```

同时，后端资料预览会从 `normalized.json` 读取结构化 `images`，根据 `caption`、`row_context`、`column_context` 和 `cell_text` 选择更匹配的问题图片。

## 长文本处理

超过长度上限的文本会按中文句末标点拆分，并保留少量重叠内容，避免跨句信息断裂。

默认参数：

- 单块长度：约 1400 字符
- 重叠长度：约 180 字符

## source_type 约定

| 值 | 含义 |
| --- | --- |
| `normative` | 规范正文 |
| `commentary` | 条文说明 |
| `normative_table` | 正文表格 |
| `commentary_table` | 条文说明表格 |
| `normative_image` | 正文图片位置 |
| `commentary_image` | 条文说明图片位置 |

回答生成时会优先信任 `normative`，但仍保留说明区作为解释性证据。

## 查看切块样式

可以用脚本查看某份文档已经入库的 chunk：

```powershell
cd E:\lmq\RAG_ZB
.\backend\.venv\Scripts\python.exe scripts\show_chunks.py --document-id b7c7d518-3b70-4122-8f07-1faab100fc3a
```

只看包含某个关键词的 chunk：

```powershell
.\backend\.venv\Scripts\python.exe scripts\show_chunks.py `
  --document-id b7c7d518-3b70-4122-8f07-1faab100fc3a `
  --contains 石膏
```

如果修改了切块代码但还没有重建索引，可以直接从 `normalized.json` 用当前代码预览 fresh 切块结果：

```powershell
.\backend\.venv\Scripts\python.exe scripts\show_chunks.py `
  --parsed-dir storage\parsed\b7c7d518-3b70-4122-8f07-1faab100fc3a `
  --fresh `
  --contains 石膏
```

两种模式的区别：

| 模式 | 含义 |
| --- | --- |
| `--document-id` | 查看 SQLite 当前已经入库的 chunk，代表真实检索正在使用的索引 |
| `--fresh --parsed-dir` | 不读数据库，直接用当前 `chunking.py` 从 `normalized.json` 重新切块，适合验证新规则 |

如果 fresh 结果正确，但数据库结果还是旧样式，需要重启后端并调用 `reindex` 重建该文档索引。

## 维护注意

- 条款号是规范问答最重要的定位锚点，正则调整后必须补充测试。
- 正文与条文说明必须区分，结论优先使用正文证据。
- 表格不宜和上下文强行合并，否则会影响页码和引用准确性。
- 表格内图片依赖解析阶段写入的 `PageBlock.images`；切分阶段不要丢失页码、表格类型和图片路径提示。
- 切分规则改动后建议用 `reindex` 复用解析结果验证，不必反复上传 PDF。

## 相关测试

```powershell
cd E:\lmq\RAG_ZB
E:\lmq\RAG_ZB\.tools\uv\bin\uv.exe run --project backend pytest backend\tests\test_chunking.py -q
```
