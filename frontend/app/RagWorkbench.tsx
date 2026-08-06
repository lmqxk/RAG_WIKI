"use client";

import { ChangeEvent, FormEvent, useEffect, useMemo, useRef, useState } from "react";

import { MarkdownMessage } from "@/components/markdown-message";

const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://127.0.0.1:8000";

type Document = {
  id: string;
  filename: string;
  title: string;
  standard_no: string | null;
  version: string | null;
  status: string;
  page_count: number;
  file_size: number;
  needs_ocr: boolean;
  parser_name: string | null;
  error: string | null;
};

type Job = {
  id: string;
  document_id: string;
  status: string;
  stage: string;
  progress: number;
  message: string;
  error: string | null;
};

type Citation = {
  index: number;
  chunk_id: string;
  document_id: string;
  document_title: string;
  standard_no: string | null;
  version: string | null;
  clause_no: string | null;
  chapter_path: string;
  pdf_page: number;
  printed_page: string | null;
  quote: string;
  score: number;
  source_type: string;
  preview_image_url: string | null;
  preview_label: string | null;
};

type ChatResult = {
  answer: string;
  citations: Citation[];
  evidence_status: "sufficient" | "partial" | "insufficient";
  query_type: string;
  used_external_llm: boolean;
  trace_id: string;
};

type Message = {
  id: string;
  role: "user" | "assistant";
  content: string;
  result?: ChatResult;
};

type Health = {
  status: string;
  llm_configured: boolean;
  embedding_configured: boolean;
  parser_available?: boolean;
  mineru_available: boolean;
  document_pipeline?: string;
};

const STATUS_LABEL: Record<string, string> = {
  QUEUED: "等待处理",
  PARSING: "解析中",
  INDEXING: "索引中",
  READY: "可检索",
  FAILED: "处理失败",
};

const SUGGESTIONS = [
  "对比两份规范中工业建筑耐火等级要求的变化",
  "总结规范中关于消防车道的主要要求",
  "5.2.1 条分别规定了什么？",
];

function formatBytes(bytes: number) {
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function parserDisplayName(parserName: string | null) {
  if (!parserName) return "等待解析";
  const normalized = parserName.toLowerCase();
  if (normalized === "mineru" || normalized === "opendatalab-pdf-extract-kit") {
    return "PDF-Extract-Kit 1.0";
  }
  if (normalized === "pymupdf") return "PyMuPDF";
  if (normalized === "rapidocr") return "RapidOCR";
  if (normalized.startsWith("mineru-")) return "MinerU VLM";
  return parserName;
}

function pipelineDisplayName(pipeline?: string) {
  const normalized = (pipeline || "pdf-extract-kit").toLowerCase();
  if (
    normalized === "pdf-extract-kit" ||
    normalized === "pdf-extract-kit-1.0" ||
    normalized === "opendatalab-pdf-extract-kit"
  ) {
    return "PDF-Extract-Kit";
  }
  if (normalized === "mineru" || normalized === "mineru-vlm" || normalized === "vlm") {
    return "MinerU VLM";
  }
  if (normalized === "hybrid" || normalized === "hybrid-engine") return "Hybrid";
  if (normalized === "rapidocr") return "RapidOCR";
  if (normalized === "pymupdf" || normalized === "native") return "PyMuPDF";
  return pipeline || "解析器";
}

function mediaUrl(path: string) {
  return path.startsWith("http") ? path : `${API_BASE}${path}`;
}

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, init);
  if (!response.ok) {
    const payload = await response.json().catch(() => null);
    throw new Error(payload?.detail ?? `请求失败（${response.status}）`);
  }
  return response.json();
}

async function streamChat(
  payload: { question: string; document_ids?: string[] },
  onDelta: (text: string) => void,
): Promise<ChatResult> {
  const response = await fetch(`${API_BASE}/api/chat/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!response.ok || !response.body) {
    const fallback = await response.json().catch(() => null);
    throw new Error(fallback?.detail ?? `请求失败（${response.status}）`);
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let finalResult: ChatResult | null = null;
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split("\n");
    buffer = lines.pop() ?? "";
    for (const line of lines) {
      if (!line.trim()) continue;
      const event = JSON.parse(line);
      if (event.event === "delta") {
        onDelta(String(event.text ?? ""));
      } else if (event.event === "done") {
        finalResult = event.result as ChatResult;
      } else if (event.event === "error") {
        throw new Error(String(event.message ?? "问答失败"));
      }
    }
  }
  if (!finalResult) throw new Error("问答流中断，未收到完成事件");
  return finalResult;
}

export function RagWorkbench() {
  const [documents, setDocuments] = useState<Document[]>([]);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [jobs, setJobs] = useState<Record<string, Job>>({});
  const [health, setHealth] = useState<Health | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [question, setQuestion] = useState("");
  const [uploading, setUploading] = useState(false);
  const [answering, setAnswering] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [activeResult, setActiveResult] = useState<ChatResult | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const chatEndRef = useRef<HTMLDivElement>(null);

  const readyDocuments = useMemo(
    () => documents.filter((document) => document.status === "READY"),
    [documents],
  );

  const loadDocuments = async () => {
    const data = await api<Document[]>("/api/documents");
    setDocuments(data);
    setSelectedIds((current) => {
      const readyIds = new Set(data.filter((item) => item.status === "READY").map((item) => item.id));
      const kept = new Set([...current].filter((id) => readyIds.has(id)));
      return kept.size === 0 && readyIds.size > 0 ? readyIds : kept;
    });
  };

  useEffect(() => {
    Promise.all([loadDocuments(), api<Health>("/api/health").then(setHealth)]).catch((error) =>
      setNotice(error.message),
    );
  }, []);

  useEffect(() => {
    const activeJobs = Object.values(jobs).filter(
      (job) => job.status === "QUEUED" || job.status === "RUNNING",
    );
    if (activeJobs.length === 0) return;
    const timer = window.setInterval(async () => {
      const updated = await Promise.all(
        activeJobs.map((job) => api<Job>(`/api/jobs/${job.id}`)),
      ).catch((error) => {
        setNotice(error.message);
        return [];
      });
      if (!updated.length) return;
      setJobs((current) => {
        const next = { ...current };
        updated.forEach((job) => {
          next[job.document_id] = job;
        });
        return next;
      });
      if (updated.some((job) => job.status === "COMPLETED" || job.status === "FAILED")) {
        await loadDocuments();
      }
    }, 1500);
    return () => window.clearInterval(timer);
  }, [jobs]);

  useEffect(() => {
    chatEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, answering]);

  const uploadFile = async (file: File) => {
    if (!file.name.toLowerCase().endsWith(".pdf")) {
      setNotice("当前只支持 PDF 文件。");
      return;
    }
    setUploading(true);
    setNotice(null);
    const form = new FormData();
    form.append("file", file);
    try {
      const result = await api<{ document: Document; job_id: string; duplicate: boolean }>(
        "/api/documents",
        { method: "POST", body: form },
      );
      const job = await api<Job>(`/api/jobs/${result.job_id}`);
      setJobs((current) => ({ ...current, [result.document.id]: job }));
      await loadDocuments();
      setNotice(result.duplicate ? "该文件已存在，已定位到原文档。" : "上传完成，已进入解析队列。");
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "上传失败");
    } finally {
      setUploading(false);
      if (fileInputRef.current) fileInputRef.current.value = "";
    }
  };

  const onFileChange = (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    if (file) void uploadFile(file);
  };

  const toggleDocument = (id: string) => {
    setSelectedIds((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const submitQuestion = async (event?: FormEvent) => {
    event?.preventDefault();
    const trimmed = question.trim();
    if (!trimmed || answering) return;
    if (!readyDocuments.length) {
      setNotice("请先上传并完成至少一份规范的解析。");
      return;
    }
    const assistantId = crypto.randomUUID();
    setMessages((current) => [
      ...current,
      { id: crypto.randomUUID(), role: "user", content: trimmed },
      { id: assistantId, role: "assistant", content: "" },
    ]);
    setQuestion("");
    setAnswering(true);
    setNotice(null);
    const requestPayload = {
      question: trimmed,
      document_ids: selectedIds.size ? [...selectedIds] : undefined,
    };
    try {
      const result = await streamChat(requestPayload, (delta) => {
        setMessages((current) =>
          current.map((message) =>
            message.id === assistantId
              ? { ...message, content: `${message.content}${delta}` }
              : message,
          ),
        );
      });
      setActiveResult(result);
      setMessages((current) =>
        current.map((message) =>
          message.id === assistantId
            ? { ...message, content: result.answer, result }
            : message,
        ),
      );
    } catch (error) {
      try {
        const result = await api<ChatResult>("/api/chat", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(requestPayload),
        });
        setActiveResult(result);
        setMessages((current) =>
          current.map((message) =>
            message.id === assistantId
              ? { ...message, content: result.answer, result }
              : message,
          ),
        );
      } catch (fallbackError) {
        const text = fallbackError instanceof Error ? fallbackError.message : "问答失败";
        setMessages((current) =>
          current.map((message) =>
            message.id === assistantId ? { ...message, content: text } : message,
          ),
        );
      }
    } finally {
      setAnswering(false);
    }
  };

  return (
    <main className="app-shell">
      <header className="topbar">
        <div className="brand">
          <span className="brand-mark">规</span>
          <div>
            <strong>规智库</strong>
            <span>Document Intelligence · Hybrid RAG</span>
          </div>
        </div>
        <div className="system-strip">
          <span className={`status-dot ${health?.status === "ok" ? "online" : ""}`} />
          <span>{health?.status === "ok" ? "服务正常" : "正在连接"}</span>
          <i />
          <span>
            {(health?.parser_available ?? health?.mineru_available)
              ? `${pipelineDisplayName(health?.document_pipeline)} 可用`
              : `${pipelineDisplayName(health?.document_pipeline)} 未就绪`}
          </span>
          <i />
          <span>{health?.llm_configured ? "外部模型已接入" : "离线资料模式"}</span>
        </div>
      </header>

      <section className="workspace">
        <LibraryPanel
          documents={documents}
          selectedIds={selectedIds}
          jobs={jobs}
          uploading={uploading}
          dragging={dragging}
          fileInputRef={fileInputRef}
          onFileChange={onFileChange}
          onUpload={uploadFile}
          onToggle={toggleDocument}
          onDragging={setDragging}
        />

        <section className="chat-panel">
          <div className="chat-heading">
            <div>
              <span className="eyebrow">GROUNDED ANSWERS</span>
              <h1>基于条款资料，回答规范问题</h1>
            </div>
            <div className="mode-pill">知识库模式</div>
          </div>
          <Conversation
            messages={messages}
            answering={answering}
            onSuggestion={setQuestion}
            onResult={setActiveResult}
            chatEndRef={chatEndRef}
          />
          <form className="question-box" onSubmit={submitQuestion}>
            <textarea
              value={question}
              onChange={(event) => setQuestion(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter" && !event.shiftKey) {
                  event.preventDefault();
                  void submitQuestion();
                }
              }}
              placeholder="输入条款、场景或新旧版本对比问题…"
              rows={3}
            />
            <div className="question-actions">
              <span>Enter 发送 · Shift + Enter 换行</span>
              <button type="submit" disabled={!question.trim() || answering}>
                {answering ? "生成中" : "检索并回答"}
              </button>
            </div>
          </form>
          {notice && (
            <button className="notice" type="button" onClick={() => setNotice(null)}>
              {notice}<span>×</span>
            </button>
          )}
        </section>

        <EvidencePanel result={activeResult} />
      </section>
    </main>
  );
}

type LibraryProps = {
  documents: Document[];
  selectedIds: Set<string>;
  jobs: Record<string, Job>;
  uploading: boolean;
  dragging: boolean;
  fileInputRef: React.RefObject<HTMLInputElement | null>;
  onFileChange: (event: ChangeEvent<HTMLInputElement>) => void;
  onUpload: (file: File) => Promise<void>;
  onToggle: (id: string) => void;
  onDragging: (value: boolean) => void;
};

function LibraryPanel(props: LibraryProps) {
  return (
    <aside className="library-panel">
      <div className="panel-heading">
        <div>
          <span className="eyebrow">KNOWLEDGE BASE</span>
          <h2>规范知识库</h2>
        </div>
        <span className="count">{props.documents.length}</span>
      </div>
      <div
        className={`upload-zone ${props.dragging ? "dragging" : ""}`}
        onDragEnter={(event) => {
          event.preventDefault();
          props.onDragging(true);
        }}
        onDragOver={(event) => event.preventDefault()}
        onDragLeave={() => props.onDragging(false)}
        onDrop={(event) => {
          event.preventDefault();
          props.onDragging(false);
          const file = event.dataTransfer.files[0];
          if (file) void props.onUpload(file);
        }}
      >
        <input ref={props.fileInputRef} type="file" accept=".pdf" onChange={props.onFileChange} />
        <button
          type="button"
          onClick={() => props.fileInputRef.current?.click()}
          disabled={props.uploading}
        >
          <span className="upload-icon">＋</span>
          <span>{props.uploading ? "正在上传…" : "上传规范 PDF"}</span>
        </button>
        <p>单文件 ≤ 100 MB · 每次上传一份</p>
      </div>

      <div className="document-list">
        {props.documents.length === 0 ? (
          <div className="empty-library">
            <span>01</span>
            <p>上传规范后，系统会自动识别章节、条款与页码。</p>
          </div>
        ) : (
          props.documents.map((document) => {
            const job = props.jobs[document.id];
            const selectable = document.status === "READY";
            return (
              <article
                className={`document-card ${props.selectedIds.has(document.id) ? "selected" : ""}`}
                key={document.id}
              >
                <button
                  className="document-select"
                  type="button"
                  onClick={() => selectable && props.onToggle(document.id)}
                  disabled={!selectable}
                  aria-label={`选择 ${document.title}`}
                >
                  <span className="file-badge">PDF</span>
                  <span className="document-copy">
                    <strong>{document.standard_no || document.title}</strong>
                    <small>{document.title}</small>
                  </span>
                  <span className={`check ${props.selectedIds.has(document.id) ? "checked" : ""}`}>
                    {props.selectedIds.has(document.id) ? "✓" : ""}
                  </span>
                </button>
                <div className="document-meta">
                  <span>{document.page_count ? `${document.page_count} 页` : formatBytes(document.file_size)}</span>
                  <span>{parserDisplayName(document.parser_name)}</span>
                  <span className={`document-status ${document.status.toLowerCase()}`}>
                    {STATUS_LABEL[document.status] || document.status}
                  </span>
                </div>
                {job && job.status !== "COMPLETED" && (
                  <div className="job-progress">
                    <div><span>{job.message}</span><b>{Math.round(job.progress)}%</b></div>
                    <progress value={job.progress} max="100" />
                  </div>
                )}
                {job?.error && <p className="document-error">{job.error}</p>}
              </article>
            );
          })
        )}
      </div>
      <div className="scope-note">
        <span>当前检索范围</span>
        <strong>
          {props.selectedIds.size ? `${props.selectedIds.size} 份已选规范` : "全部可用规范"}
        </strong>
      </div>
    </aside>
  );
}

type ConversationProps = {
  messages: Message[];
  answering: boolean;
  onSuggestion: (value: string) => void;
  onResult: (result: ChatResult | null) => void;
  chatEndRef: React.RefObject<HTMLDivElement | null>;
};

function Conversation(props: ConversationProps) {
  const hasPendingAssistant = props.messages.some(
    (message) => message.role === "assistant" && !message.result,
  );

  return (
    <div className="conversation">
      {props.messages.length === 0 ? (
        <div className="welcome">
          <span className="welcome-index">RAG / 01</span>
          <h3>从准确定位，到跨版本综合判断</h3>
          <p>
            系统同时检索条款编号、关键词与语义结果，再重新排序。回答中的每项依据均可回到原 PDF 页。
          </p>
          <div className="suggestions">
            {SUGGESTIONS.map((suggestion, index) => (
              <button type="button" key={suggestion} onClick={() => props.onSuggestion(suggestion)}>
                <span>0{index + 1}</span>{suggestion}
              </button>
            ))}
          </div>
        </div>
      ) : (
        props.messages.map((message) => (
          <article className={`message ${message.role}`} key={message.id}>
            <div className="message-label">
              <span>{message.role === "user" ? "你" : "智"}</span>
              <strong>{message.role === "user" ? "问题" : "基于规范的回答"}</strong>
              {message.result && (
                <em className={message.result.evidence_status}>
                  {message.result.evidence_status === "sufficient"
                    ? "资料充分"
                    : message.result.evidence_status === "partial"
                      ? "资料部分充分"
                      : "资料不足"}
                </em>
              )}
            </div>
            <div className="message-content">
              {message.role === "assistant" && !message.content && !message.result ? (
                <PendingAnswer />
              ) : (
                <MarkdownMessage content={message.content} />
              )}
            </div>
            {message.result && (
              <CitationPreviewStrip answer={message.content} result={message.result} />
            )}
            {message.result?.citations.length ? (
              <button type="button" className="citation-jump" onClick={() => props.onResult(message.result ?? null)}>
                查看 {message.result.citations.length} 条原文资料 →
              </button>
            ) : null}
          </article>
        ))
      )}
      {props.answering && !hasPendingAssistant && (
        <article className="message assistant loading-answer">
          <div className="message-label"><span>智</span><strong>检索与核对中</strong></div>
          <div className="thinking-line"><i /><i /><i /></div>
        </article>
      )}
      <div ref={props.chatEndRef} />
    </div>
  );
}

function PendingAnswer() {
  const steps = ["规划检索中", "核对资料中", "组织回答中"];
  const [stepIndex, setStepIndex] = useState(0);

  useEffect(() => {
    const timer = window.setInterval(() => {
      setStepIndex((current) => Math.min(current + 1, steps.length - 1));
    }, 5000);
    return () => window.clearInterval(timer);
  }, [steps.length]);

  return (
    <div className="pending-answer">
      <span>{steps[stepIndex]}</span>
      <span className="thinking-line" aria-hidden="true"><i /><i /><i /></span>
    </div>
  );
}

function CitationPreviewStrip({ answer, result }: { answer: string; result: ChatResult }) {
  const citedIndexes = Array.from(answer.matchAll(/\[(\d+)\]/g), (match) => Number(match[1]));
  const citedPreview = citedIndexes
    .map((index) => result.citations.find((citation) => citation.index === index))
    .find((citation) => citation?.preview_image_url);
  const previews = [citedPreview ?? null]
    .filter((citation): citation is Citation => Boolean(citation?.preview_image_url));
  if (!previews.length) return null;
  return (
    <div className="answer-preview-strip">
      {previews.map((citation) => (
        <a
          key={`${citation.chunk_id}-preview`}
          className="answer-preview-card"
          href={`${API_BASE}/api/documents/${citation.document_id}/file#page=${citation.pdf_page}`}
          target="_blank"
          rel="noreferrer"
        >
          <img
            src={mediaUrl(citation.preview_image_url!)}
            alt={citation.preview_label || "检索预览"}
            loading="lazy"
          />
          <span>
            [{citation.index}] {citation.preview_label || "图表预览"} · PDF 第 {citation.pdf_page} 页
          </span>
        </a>
      ))}
    </div>
  );
}

function EvidencePanel({ result }: { result: ChatResult | null }) {
  return (
    <aside className="evidence-panel">
      <div className="panel-heading evidence-heading">
        <div><span className="eyebrow">SOURCE MATERIAL</span><h2>原文资料</h2></div>
        <span className="count">{result?.citations.length ?? 0}</span>
      </div>
      {!result?.citations.length ? (
        <div className="empty-evidence">
          <div className="locator"><i /><i /><span>引</span></div>
          <h3>答案的资料会出现在这里</h3>
          <p>包含规范名称、版本、条款号、原文摘录和 PDF 页码。</p>
        </div>
      ) : (
        <div className="citation-list">
          {result.citations.map((citation) => (
            <article className="citation-card" key={citation.chunk_id}>
              <div className="citation-topline"><span>[{citation.index}]</span><em>{citation.score.toFixed(3)}</em></div>
              <h3>{citation.document_title}</h3>
              <div className="citation-tags">
                {citation.standard_no && <span>{citation.standard_no}</span>}
                {citation.version && <span>{citation.version} 版</span>}
                <span>
                  {citation.source_type.startsWith("normative") ? "规范正文" : "条文说明"}
                </span>
              </div>
              <strong className="clause">{citation.clause_no || citation.chapter_path || "相关内容"}</strong>
              <blockquote>{citation.quote}</blockquote>
              <a href={`${API_BASE}/api/documents/${citation.document_id}/file#page=${citation.pdf_page}`} target="_blank" rel="noreferrer">
                预览 PDF 第 {citation.pdf_page} 页
                {citation.printed_page ? ` · 纸面第 ${citation.printed_page} 页` : ""}
                <span>↗</span>
              </a>
            </article>
          ))}
        </div>
      )}
      <div className="pipeline-mini">
        <span>检索链路</span>
        <div><b>BM25</b><i /><b>Vector</b><i /><b>Rerank</b></div>
      </div>
    </aside>
  );
}
