# 04 - 文档解析

## 模块定位

文档解析模块默认使用 OpenDataLab PDF-Extract-Kit 1.0 pipeline 对整份 PDF 做 Document Understanding，并把 PDF 转换成带页码、文本块、表格、图片位置、版面信息和 Markdown 的结构化结果。

主要文件：

- `backend/src/backend/parser.py`
- `backend/src/backend/domain.py`

## 技术组件

| 组件 | 用途 |
| --- | --- |
| PyMuPDF / `fitz` | 检查 PDF、读取文本层、提取 blocks、渲染扫描页 |
| NumPy | 接收渲染后的页面图像数组 |
| OpenDataLab PDF-Extract-Kit 1.0 | 默认全量 PDF 解析，适合复杂版式、表格、图片、公式 |
| RapidOCR | 轻量 fallback，仅适合扫描 PDF 文本识别 |
| Markdown | 保存可读解析结果 |
| JSON | 保存标准化结构结果 |

## 输入输出

输入：

- PDF 文件路径
- 解析输出目录
- 进度回调函数

输出：

- `ParsedDocument.pages`
- `ParsedDocument.blocks`
- `ParsedDocument.markdown`
- `ParsedDocument.parser_name`
- `ParsedDocument.needs_ocr`

## 解析流程

```text
inspect_pdf()
  -> 检查页数是否超过上限
  -> 抽样判断是否存在文本层
  -> 默认调用 OpenDataLab PDF-Extract-Kit pipeline 全量解析
  -> PDF-Extract-Kit 不可用且 PDF 有文本层时 fallback 到 NativePdfParser
  -> PDF-Extract-Kit 不可用且 PDF 是扫描件时抛出解析错误
```

### 默认 PDF-Extract-Kit 全量解析

只要 `RAG_DOCUMENT_PIPELINE=pdf-extract-kit` 且解析命令可用，系统会把文本型和扫描型 PDF 都交给 OpenDataLab PDF-Extract-Kit 1.0 pipeline。

这样做的理由：

- PyMuPDF 对纯文本层很快，但无法可靠恢复复杂表格结构。
- PyMuPDF 对图片型表格只能看到图片，不能抽出表格语义。
- PDF-Extract-Kit 可以输出 Markdown、`content_list.json`、表格内容、图片 caption/path 和 bbox，适合作为 RAG 入库源。

PDF-Extract-Kit 输出中会被纳入可检索文本的字段：

- `text`
- `table_caption`
- `table_body`
- `table_footnote`
- `img_caption`
- `image_caption`
- `img_footnote`
- `image_footnote`
- `img_path`
- `image_path`

### PyMuPDF fallback

`NativePdfParser` 使用 `page.get_text("text")` 和 `page.get_text("blocks", sort=True)` 提取文本块，并把每页写入 Markdown 注释：

```text
<!-- PDF_PAGE:1 -->
```

这类解析速度最快，但只作为 PDF-Extract-Kit 不可用时的文本型 PDF fallback。

### RapidOCR fallback

`inspect_pdf()` 会抽样多页文本层，如果有效文本页不足，则判定为扫描型 PDF。

如果 `RAG_DOCUMENT_PIPELINE=rapidocr`，PDF 会走 `RapidOcrParser`：

- 按 `RAG_OCR_RENDER_DPI` 渲染单页。
- 单页 OCR，避免一次性加载整本文档造成内存压力。
- 过滤低置信度文本。
- 按 bbox 的 y、x 顺序重排文本行。

RapidOCR 只能解决文字识别，表格结构、图片位置和 Markdown 版面效果通常弱于 PDF-Extract-Kit。

## 结构化字段

| 字段 | 含义 |
| --- | --- |
| `page` | PDF 物理页码，从 1 开始 |
| `text` | 当前块文本 |
| `block_type` | 文本、图片、表格等类型 |
| `bbox` | 页面坐标，用于保留阅读顺序和版面信息 |
| `level` | 部分解析器返回的标题层级 |
| `printed_page` | 纸面页码，通常来自页脚 |
| `images` | 当前块关联的图片列表，主要用于表格内图片和普通图片预览 |

### 表格内图片结构

PDF-Extract-Kit 的表格 HTML 中可能包含 `<img src="...">`。解析阶段会把这些图片从表格单元格里抽出来，写入当前 `PageBlock.images`。

典型结构：

```json
{
  "page": 2,
  "type": "table",
  "text": "表格标题：续附表2 ...",
  "images": [
    {
      "path": "images/c0fb25e649779c5d07746aed829aaa20e71dbcb18263c3cd9f021acd04fa4390.jpg",
      "caption": "橡檩屋顶截面",
      "row_context": "屋顶承重构件",
      "column_context": "截面图和结构厚度或截面最小尺寸(mm)",
      "cell_text": "橡檩屋顶截面0.50轻型木桁架屋顶截面",
      "order": 1
    }
  ]
}
```

字段含义：

| 字段 | 含义 |
| --- | --- |
| `path` | 解析产物中的相对图片路径 |
| `caption` | 图片 caption；表格单元格内图片优先使用图片前后的邻近文字 |
| `row_context` | 图片所在行的上下文，通常来自首列或行标题 |
| `column_context` | 图片所在列的表头 |
| `cell_text` | 图片所在单元格的完整文本 |
| `order` | 同一单元格内图片顺序 |

这样做是为了让“屋顶承重构件截面图”“轻型木桁架屋顶截面”等问题能把表格文字和对应图片关联起来，而不是只知道这一页有图片。

## 解析结果落盘

入库模块会把解析结果保存到：

```text
storage/parsed/{document_id}/normalized.json
storage/parsed/{document_id}/document.md
```

`normalized.json` 是后续重建索引的稳定输入；`document.md` 主要用于人工排查解析质量。

## 重新解析已有文档

如果改的是 PDF 解析器、PDF-Extract-Kit 输出转换、OCR、表格解析、表格内图片提取或 `normalized.json` 生成逻辑，只调用 `reindex` 不够，需要重新解析已有文档。

重新解析接口：

```text
POST /api/documents/{document_id}/reparse
```

这个接口会替换该文档 ID 对应的已上传 PDF，并重新执行完整链路：

```text
替换 storage/uploads/{document_id}.pdf
  -> 重新解析 PDF
  -> 重新生成 normalized.json 和 document.md
  -> 重新切分
  -> 重写 SQLite FTS5 和 Qdrant 索引
```

PowerShell 里 `curl` 默认是 `Invoke-WebRequest` 别名，不支持 `curl -X/-F` 这种参数。需要显式使用 `curl.exe`：

```powershell
curl.exe -X POST "http://127.0.0.1:8000/api/documents/{document_id}/reparse" `
  -F "file=@E:\docs\updated.pdf"
```

当前文档示例：

```powershell
curl.exe -X POST "http://127.0.0.1:8000/api/documents/f8f7c0aa-6367-4950-b347-c00f1c3c8bd4/reparse" `
  -F "file=@E:\lmq\规范\《建筑设计防火规范》GB50016-2014（2018版）.pdf"
```
查任务进度，把返回的 job_id 换进去：

```powershell
curl.exe http://127.0.0.1:8000/api/jobs/{job_id}
```

如果只改了切分、检索词、重排或向量写入逻辑，PDF 解析结果本身没变，可以用 `reindex`，它只复用已有 `storage/parsed/{document_id}/normalized.json` 重建索引，不会重新跑 PDF-Extract-Kit/OCR。

## 常见问题

- 如果 PDF 页数超过 `RAG_MAX_PDF_PAGES`，解析前会直接失败。
- 扫描件识别质量受渲染 DPI、图片清晰度、表格复杂度影响。
- 纸面页码和 PDF 物理页码不是同一个概念，引用跳转使用 PDF 物理页码。
- 表格会以 `normative_table` 或 `commentary_table` 入库。
- 图片位置会以 `normative_image` 或 `commentary_image` 入库；如果 PDF-Extract-Kit 提供 caption/path，会一起进入可检索文本。
- 表格内图片的 `images` 结构只有重新解析后才会写入旧文档的 `normalized.json`；只重建索引不能补出这些字段。

## 关键配置

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `RAG_MAX_PDF_PAGES` | `600` | PDF 页数上限 |
| `RAG_DOCUMENT_PIPELINE` | `pdf-extract-kit` | 文档解析管线别名，推荐使用这个变量切换解析链路 |
| `RAG_SCAN_PARSER` | `pdf-extract-kit` | 旧解析器入口，保留兼容，不建议再作为管线选择项 |
| `RAG_OCR_RENDER_DPI` | `180` | OCR 渲染 DPI |
| `RAG_MINERU_COMMAND` | 空 | 解析命令路径，底层仍使用 `mineru.exe` CLI |
| `RAG_MINERU_BACKEND` | 空 | 旧 CLI backend 覆盖项，仅兼容历史配置；新配置优先用 `RAG_DOCUMENT_PIPELINE` |
| `RAG_MINERU_METHOD` | `auto` | 解析 method |
| `RAG_MINERU_OCR_LANG` | `ch` | OCR 语言 |

`RAG_DOCUMENT_PIPELINE` 当前支持的常用别名：

| 别名 | 实际管线 |
| --- | --- |
| `pdf-extract-kit` | OpenDataLab PDF-Extract-Kit 1.0，对应 CLI backend `pipeline` |
| `mineru` / `mineru-vlm` / `vlm` | MinerU VLM，对应 CLI backend `vlm-engine` |
| `hybrid` | MinerU hybrid，对应 CLI backend `hybrid-engine` |
| `rapidocr` | 直接使用 RapidOCR fallback |
| `pymupdf` / `native` | 直接使用 PyMuPDF 文本层提取 |
