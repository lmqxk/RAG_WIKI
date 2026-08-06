"use client";

import {
  ChangeEvent,
  DragEvent,
  FormEvent,
  RefObject,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  AlertCircle,
  ArrowUpRight,
  BookOpen,
  Bot,
  Check,
  ChevronRight,
  CircleHelp,
  Database,
  FileCheck2,
  FileSearch,
  FileText,
  Gauge,
  Home,
  Layers3,
  Menu,
  PanelRightOpen,
  Search,
  Send,
  Settings,
  ShieldCheck,
  Sparkles,
  UploadCloud,
  X,
  Zap,
} from "lucide-react";

import { Alert, AlertDescription } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Progress } from "@/components/ui/progress";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Separator } from "@/components/ui/separator";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { Skeleton } from "@/components/ui/skeleton";
import { Textarea } from "@/components/ui/textarea";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
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
  "对比两份规范对附设燃油或燃气锅炉房的设置要求",
  "总结规范中关于消防车道的主要要求",
  "建筑防火通用规范 4.1.4 条规定了什么？",
];

const QUERY_LABEL: Record<string, string> = {
  fact: "事实问答",
  clause: "条款查询",
  summary: "主题总结",
  comparison: "综合对比",
};

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

export function RagDashboard() {
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
  const [evidenceOpen, setEvidenceOpen] = useState(false);
  const [mobileNavOpen, setMobileNavOpen] = useState(false);
  const [activeView, setActiveView] = useState("workbench");
  const [documentSearch, setDocumentSearch] = useState("");
  const fileInputRef = useRef<HTMLInputElement>(null);
  const chatEndRef = useRef<HTMLDivElement>(null);

  const readyDocuments = useMemo(
    () => documents.filter((document) => document.status === "READY"),
    [documents],
  );
  const filteredDocuments = useMemo(() => {
    const keyword = documentSearch.trim().toLowerCase();
    if (!keyword) return documents;
    return documents.filter((document) =>
      [document.title, document.standard_no, document.filename]
        .filter(Boolean)
        .some((value) => String(value).toLowerCase().includes(keyword)),
    );
  }, [documentSearch, documents]);

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
      setNotice(result.duplicate ? "该文件已经存在，已定位到原文档。" : "上传完成，已进入解析队列。");
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

  const navigate = (view: string) => {
    setActiveView(view);
    setMobileNavOpen(false);
  };

  return (
    <div className="min-h-screen bg-muted/40">
      <DesktopNavigation activeView={activeView} onNavigate={navigate} health={health} />

      <div className="min-h-screen lg:pl-56">
        <TopBar
          activeView={activeView}
          health={health}
          onOpenNavigation={() => setMobileNavOpen(true)}
        />

        <main className="mx-auto w-full max-w-[1600px] px-4 py-5 sm:px-6 lg:px-8">
          {notice && (
            <Alert className="mb-5 bg-card">
              <AlertCircle />
              <AlertDescription className="flex items-center justify-between gap-4">
                <span>{notice}</span>
                <Button
                  variant="ghost"
                  size="icon-xs"
                  type="button"
                  onClick={() => setNotice(null)}
                  aria-label="关闭提示"
                >
                  <X />
                </Button>
              </AlertDescription>
            </Alert>
          )}

          <div className="mb-5">
            <h1 className="text-2xl font-semibold tracking-tight sm:text-3xl">
              {activeView === "library" ? "规范知识库" : "规范智能问答工作台"}
            </h1>
            <p className="mt-1 text-sm text-muted-foreground">
              {activeView === "library"
                ? "管理规范、解析状态与问答范围"
                : "从精确条款定位，到跨版本综合对比"}
            </p>
          </div>

          {activeView === "workbench" ? (
            <>
              <MetricGrid
                documents={documents}
                selectedCount={selectedIds.size}
                citationCount={activeResult?.citations.length ?? 0}
              />
              <div className="mt-5 grid min-w-0 gap-5 xl:grid-cols-[minmax(0,1fr)_340px]">
                <ChatWorkspace
                  messages={messages}
                  answering={answering}
                  question={question}
                  selectedCount={selectedIds.size}
                  llmConfigured={health?.llm_configured ?? false}
                  chatEndRef={chatEndRef}
                  onQuestion={setQuestion}
                  onSubmit={submitQuestion}
                  onSuggestion={setQuestion}
                  onOpenEvidence={(result) => {
                    setActiveResult(result);
                    setEvidenceOpen(true);
                  }}
                />
                <KnowledgeScope
                  documents={readyDocuments}
                  selectedIds={selectedIds}
                  activeResult={activeResult}
                  onToggle={toggleDocument}
                  onUpload={() => fileInputRef.current?.click()}
                  onOpenEvidence={() => setEvidenceOpen(true)}
                />
              </div>
            </>
          ) : (
            <LibraryWorkspace
              documents={filteredDocuments}
              allDocumentCount={documents.length}
              search={documentSearch}
              jobs={jobs}
              selectedIds={selectedIds}
              dragging={dragging}
              uploading={uploading}
              fileInputRef={fileInputRef}
              onSearch={setDocumentSearch}
              onFileChange={onFileChange}
              onUpload={uploadFile}
              onToggle={toggleDocument}
              onDragging={setDragging}
            />
          )}
        </main>
      </div>

      <input
        className="hidden"
        ref={fileInputRef}
        type="file"
        accept=".pdf"
        onChange={onFileChange}
      />

      <Sheet open={mobileNavOpen} onOpenChange={setMobileNavOpen}>
        <SheetContent side="left" className="w-64 p-0">
          <SheetHeader className="sr-only">
            <SheetTitle>主导航</SheetTitle>
            <SheetDescription>切换规范问答系统功能</SheetDescription>
          </SheetHeader>
          <NavigationBody activeView={activeView} onNavigate={navigate} health={health} />
        </SheetContent>
      </Sheet>

      <EvidenceSheet
        open={evidenceOpen}
        onOpenChange={setEvidenceOpen}
        result={activeResult}
      />
    </div>
  );
}

function DesktopNavigation({
  activeView,
  onNavigate,
  health,
}: {
  activeView: string;
  onNavigate: (view: string) => void;
  health: Health | null;
}) {
  return (
    <aside className="fixed inset-y-0 left-0 z-30 hidden w-56 border-r bg-sidebar lg:block">
      <NavigationBody activeView={activeView} onNavigate={onNavigate} health={health} />
    </aside>
  );
}

function NavigationBody({
  activeView,
  onNavigate,
  health,
}: {
  activeView: string;
  onNavigate: (view: string) => void;
  health: Health | null;
}) {
  const items = [
    { id: "workbench", label: "智能问答", icon: Bot },
    { id: "library", label: "知识库", icon: Database },
  ];
  return (
    <div className="flex h-full flex-col p-3">
      <div className="flex h-14 items-center gap-3 px-2">
        <div className="flex size-9 items-center justify-center rounded-xl bg-white shadow-sm ring-1 ring-border">
          <img src="/favicon.svg" alt="" className="size-6" />
        </div>
        <div className="min-w-0">
          <div className="truncate text-base font-semibold">规智库</div>
          <div className="truncate text-[11px] text-muted-foreground">SPEC INTELLIGENCE</div>
        </div>
      </div>
      <Separator className="my-3" />
      <nav className="flex flex-col gap-1">
        <p className="px-2 pb-2 text-[11px] font-medium tracking-wider text-muted-foreground">
          工作空间
        </p>
        {items.map((item) => {
          const Icon = item.icon;
          const active = activeView === item.id;
          return (
            <Button
              key={item.id}
              variant={active ? "secondary" : "ghost"}
              className="h-10 justify-start px-3"
              type="button"
              onClick={() => onNavigate(item.id)}
            >
              <Icon data-icon="inline-start" />
              {item.label}
              {active && <ChevronRight data-icon="inline-end" className="ml-auto" />}
            </Button>
          );
        })}
        <Tooltip>
          <TooltipTrigger asChild>
            <Button variant="ghost" className="h-10 justify-start px-3" disabled>
              <Gauge data-icon="inline-start" />
              检索评测
              <Badge variant="outline" className="ml-auto text-[10px]">规划中</Badge>
            </Button>
          </TooltipTrigger>
          <TooltipContent>后续接入评测集与召回指标</TooltipContent>
        </Tooltip>
        <Button variant="ghost" className="h-10 justify-start px-3" disabled>
          <Settings data-icon="inline-start" />
          系统设置
        </Button>
      </nav>
      <div className="mt-auto rounded-xl border bg-card p-3">
        <div className="flex items-center justify-between">
          <span className="text-xs font-medium">系统状态</span>
          <span
            className={`size-2 rounded-full ${health?.status === "ok" ? "bg-primary" : "bg-muted-foreground"}`}
          />
        </div>
        <p className="mt-2 text-xs text-muted-foreground">
          {health?.status === "ok" ? "API 与本地索引运行正常" : "正在连接后端服务"}
        </p>
        <p className="mt-1 text-xs text-muted-foreground">
          {(health?.parser_available ?? health?.mineru_available)
            ? `${pipelineDisplayName(health?.document_pipeline)} 可用`
            : `${pipelineDisplayName(health?.document_pipeline)} 未就绪`}
        </p>
      </div>
    </div>
  );
}

function TopBar({
  activeView,
  health,
  onOpenNavigation,
}: {
  activeView: string;
  health: Health | null;
  onOpenNavigation: () => void;
}) {
  return (
    <header className="sticky top-0 z-20 flex h-16 items-center justify-between border-b bg-background/90 px-4 backdrop-blur sm:px-6 lg:px-8">
      <div className="flex items-center gap-3">
        <Button
          variant="outline"
          size="icon"
          type="button"
          className="lg:hidden"
          onClick={onOpenNavigation}
          aria-label="打开导航"
        >
          <Menu />
        </Button>
        <div>
          <p className="text-sm font-medium">
            {activeView === "library" ? "知识库管理" : "智能问答"}
          </p>
          <p className="hidden text-xs text-muted-foreground sm:block">
            建筑工程规范 · 技术标准 · 企业制度
          </p>
        </div>
      </div>
      <div className="flex items-center gap-2">
        <Badge variant="outline" className="hidden sm:inline-flex">
          <span className="mr-1.5 size-1.5 rounded-full bg-primary" />
          {health ? "服务正常" : "正在连接"}
        </Badge>
        <Badge variant={health?.llm_configured ? "secondary" : "outline"} className="hidden sm:inline-flex">
          {health?.llm_configured ? "LLM 已加载" : "离线资料模式"}
        </Badge>
        <Badge
          variant={(health?.parser_available ?? health?.mineru_available) ? "secondary" : "outline"}
          className="hidden sm:inline-flex"
        >
          {(health?.parser_available ?? health?.mineru_available)
            ? pipelineDisplayName(health?.document_pipeline)
            : "解析器未就绪"}
        </Badge>
        <Tooltip>
          <TooltipTrigger asChild>
            <Button variant="ghost" size="icon" type="button" aria-label="帮助">
              <CircleHelp />
            </Button>
          </TooltipTrigger>
          <TooltipContent>所有结论均可回到 PDF 原页核对</TooltipContent>
        </Tooltip>
      </div>
    </header>
  );
}

function MetricGrid({
  documents,
  selectedCount,
  citationCount,
}: {
  documents: Document[];
  selectedCount: number;
  citationCount: number;
}) {
  const ready = documents.filter((document) => document.status === "READY").length;
  const ocr = documents.filter((document) => document.needs_ocr).length;
  const metrics = [
    { label: "知识库文档", value: documents.length, detail: `${ready} 份可检索`, icon: Database },
    { label: "当前检索范围", value: selectedCount, detail: "支持多文档综合", icon: Layers3 },
    { label: "扫描文档", value: ocr, detail: "已自动识别内容", icon: FileSearch },
    { label: "本轮引用", value: citationCount, detail: "可跳转 PDF 原页", icon: ShieldCheck },
  ];
  return (
    <section className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
      {metrics.map((metric) => {
        const Icon = metric.icon;
        return (
          <Card key={metric.label} className="shadow-none">
            <CardContent className="flex items-center justify-between p-4">
              <div>
                <p className="text-xs text-muted-foreground">{metric.label}</p>
                <p className="mt-1 text-2xl font-semibold">{metric.value}</p>
                <p className="mt-1 text-xs text-muted-foreground">{metric.detail}</p>
              </div>
              <div className="flex size-10 items-center justify-center rounded-xl bg-primary/10 text-primary">
                <Icon className="size-5" />
              </div>
            </CardContent>
          </Card>
        );
      })}
    </section>
  );
}

function ChatWorkspace({
  messages,
  answering,
  question,
  selectedCount,
  llmConfigured,
  chatEndRef,
  onQuestion,
  onSubmit,
  onSuggestion,
  onOpenEvidence,
}: {
  messages: Message[];
  answering: boolean;
  question: string;
  selectedCount: number;
  llmConfigured: boolean;
  chatEndRef: RefObject<HTMLDivElement | null>;
  onQuestion: (value: string) => void;
  onSubmit: (event?: FormEvent) => Promise<void>;
  onSuggestion: (value: string) => void;
  onOpenEvidence: (result: ChatResult) => void;
}) {
  const hasPendingAssistant = messages.some(
    (message) => message.role === "assistant" && !message.result,
  );

  return (
    <Card className="min-w-0 overflow-hidden shadow-sm">
      <CardHeader className="border-b">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <CardTitle className="flex items-center gap-2">
              <Sparkles className="size-5 text-primary" />
              基于规范资料回答
            </CardTitle>
          <CardDescription className="mt-1">
              已选择 {selectedCount} 份规范，当前为 {llmConfigured ? "LLM 总结模式" : "离线资料模式"}
          </CardDescription>
          </div>
          <Badge variant="secondary">回答附原文资料</Badge>
        </div>
      </CardHeader>

      <ScrollArea className="h-[calc(100vh-420px)] min-h-[430px]">
        <CardContent className="p-5 sm:p-6">
          {messages.length === 0 ? (
            <WelcomeState onSuggestion={onSuggestion} />
          ) : (
            <div className="flex flex-col gap-5">
              {messages.map((message) => (
                <article
                  key={message.id}
                  className={`flex gap-3 ${message.role === "user" ? "justify-end" : "justify-start"}`}
                >
                  {message.role === "assistant" && (
                    <div className="flex size-8 shrink-0 items-center justify-center rounded-lg bg-primary text-primary-foreground">
                      <Bot className="size-4" />
                    </div>
                  )}
                  <div
                    className={`max-w-[88%] rounded-2xl px-4 py-3 text-sm leading-7 ${
                      message.role === "user"
                        ? "rounded-tr-sm bg-primary text-primary-foreground"
                        : "rounded-tl-sm border bg-muted/40"
                    }`}
                  >
                    {message.result && (
                      <div className="mb-2 flex flex-wrap items-center gap-2">
                        <Badge variant="outline">
                          {QUERY_LABEL[message.result.query_type] ?? message.result.query_type}
                        </Badge>
                        <Badge variant={message.result.evidence_status === "sufficient" ? "secondary" : "outline"}>
                          {message.result.evidence_status === "sufficient"
                            ? "资料充分"
                            : message.result.evidence_status === "partial"
                              ? "资料部分充分"
                              : "资料不足"}
                        </Badge>
                        <Badge variant={message.result.used_external_llm ? "secondary" : "outline"}>
                          {message.result.used_external_llm ? "LLM 总结" : "离线资料模式"}
                        </Badge>
                      </div>
                    )}
                    <div className="break-words">
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
                      <Button
                        variant="outline"
                        size="sm"
                        className="mt-3"
                        type="button"
                        onClick={() => onOpenEvidence(message.result!)}
                      >
                        <FileCheck2 data-icon="inline-start" />
                        查看 {message.result.citations.length} 条原文资料
                      </Button>
                    ) : null}
                  </div>
                </article>
              ))}
              {answering && !hasPendingAssistant && (
                <div className="flex gap-3">
                  <div className="flex size-8 shrink-0 items-center justify-center rounded-lg bg-primary text-primary-foreground">
                    <Bot className="size-4" />
                  </div>
                  <div className="w-full max-w-md rounded-2xl rounded-tl-sm border bg-muted/40 p-4">
                    <Skeleton className="h-3 w-2/3" />
                    <Skeleton className="mt-3 h-3 w-full" />
                    <Skeleton className="mt-2 h-3 w-4/5" />
                  </div>
                </div>
              )}
              <div ref={chatEndRef} />
            </div>
          )}
        </CardContent>
      </ScrollArea>

      <CardFooter className="border-t bg-muted/20 p-4">
        <form className="w-full" onSubmit={onSubmit}>
          <div className="rounded-xl border bg-background p-2 shadow-sm focus-within:ring-2 focus-within:ring-ring/30">
            <Textarea
              value={question}
              onChange={(event) => onQuestion(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter" && !event.shiftKey) {
                  event.preventDefault();
                  void onSubmit();
                }
              }}
              placeholder="输入条款、场景或新旧版本对比问题…"
              className="min-h-20 resize-none border-0 bg-transparent shadow-none focus-visible:ring-0"
            />
            <div className="flex items-center justify-between gap-3 px-1 pb-1">
              <span className="text-xs text-muted-foreground">Enter 发送 · Shift + Enter 换行</span>
              <Button type="submit" disabled={!question.trim() || answering}>
                {answering ? <Zap data-icon="inline-start" /> : <Send data-icon="inline-start" />}
                {answering ? "检索中" : "检索并回答"}
              </Button>
            </div>
          </div>
        </form>
      </CardFooter>
    </Card>
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
    <div className="flex items-center gap-3 text-muted-foreground">
      <span className="text-sm">{steps[stepIndex]}</span>
      <span className="flex gap-1" aria-hidden="true">
        <span className="size-1.5 animate-bounce rounded-full bg-current [animation-delay:-0.2s]" />
        <span className="size-1.5 animate-bounce rounded-full bg-current [animation-delay:-0.1s]" />
        <span className="size-1.5 animate-bounce rounded-full bg-current" />
      </span>
    </div>
  );
}

function CitationPreviewStrip({ answer, result }: { answer: string; result: ChatResult }) {
  const citedIndexes = Array.from(answer.matchAll(/\[(\d+)\]/g), (match) => Number(match[1]));
  const citedPreview = citedIndexes
    .map((index) => result.citations.find((citation) => citation.index === index))
    .find((citation) => citation?.preview_image_url);
  const citation = citedPreview ?? null;
  if (!citation) return null;
  return (
    <div className="mt-4 grid gap-3 sm:grid-cols-2">
        <a
          key={`${citation.chunk_id}-preview`}
          href={`${API_BASE}/api/documents/${citation.document_id}/file#page=${citation.pdf_page}`}
          target="_blank"
          rel="noreferrer"
          className="group overflow-hidden rounded-lg border bg-background text-left transition hover:border-primary/50"
        >
          <div className="aspect-[4/3] bg-muted">
            <img
              src={mediaUrl(citation.preview_image_url!)}
              alt={citation.preview_label || "检索预览"}
              className="h-full w-full object-contain"
              loading="lazy"
            />
          </div>
          <div className="flex items-center justify-between gap-2 border-t px-3 py-2 text-xs">
            <span className="truncate text-muted-foreground">
              [{citation.index}] {citation.preview_label || "图表预览"}
            </span>
            <span className="shrink-0 text-primary">PDF 第 {citation.pdf_page} 页</span>
          </div>
        </a>
    </div>
  );
}

function WelcomeState({ onSuggestion }: { onSuggestion: (value: string) => void }) {
  return (
    <div className="mx-auto flex min-h-[390px] max-w-3xl flex-col justify-center">
      <div className="mb-5 flex size-12 items-center justify-center rounded-2xl bg-primary/10 text-primary">
        <Bot className="size-6" />
      </div>
      <h2 className="text-2xl font-semibold tracking-tight">今天想核对哪一条规范？</h2>
      <p className="mt-2 max-w-2xl text-sm leading-6 text-muted-foreground">
        系统会从已选规范中查找相关条款并组织回答，每项结论都可以回到 PDF 原页核查。
      </p>
      <div className="mt-7 grid gap-3 md:grid-cols-3">
        {SUGGESTIONS.map((suggestion, index) => (
          <button
            key={suggestion}
            type="button"
            className="group flex min-h-28 flex-col justify-between rounded-xl border bg-card p-4 text-left transition hover:border-primary/40 hover:shadow-sm"
            onClick={() => onSuggestion(suggestion)}
          >
            <span className="text-xs font-medium text-primary">示例 0{index + 1}</span>
            <span className="mt-4 text-sm font-medium leading-6">{suggestion}</span>
            <ArrowUpRight className="mt-2 size-4 text-muted-foreground transition group-hover:text-primary" />
          </button>
        ))}
      </div>
    </div>
  );
}

function KnowledgeScope({
  documents,
  selectedIds,
  activeResult,
  onToggle,
  onUpload,
  onOpenEvidence,
}: {
  documents: Document[];
  selectedIds: Set<string>;
  activeResult: ChatResult | null;
  onToggle: (id: string) => void;
  onUpload: () => void;
  onOpenEvidence: () => void;
}) {
  return (
    <div className="flex flex-col gap-5">
      <Card className="shadow-none">
        <CardHeader>
          <div className="flex items-start justify-between gap-3">
            <div>
              <CardTitle className="text-base">本轮检索范围</CardTitle>
              <CardDescription>可同时选择多份规范做综合回答</CardDescription>
            </div>
            <Badge variant="secondary">{selectedIds.size}/{documents.length}</Badge>
          </div>
        </CardHeader>
        <CardContent>
          <div className="flex max-h-64 flex-col gap-2 overflow-auto pr-1">
            {documents.map((document) => (
              <label
                key={document.id}
                className="flex cursor-pointer items-start gap-3 rounded-xl border p-3 transition hover:bg-muted/40"
              >
                <Checkbox
                  checked={selectedIds.has(document.id)}
                  onCheckedChange={() => onToggle(document.id)}
                  aria-label={`选择 ${document.title}`}
                />
                <span className="min-w-0">
                  <span className="block truncate text-sm font-medium">
                    {document.standard_no || document.title}
                  </span>
                  <span className="mt-1 block truncate text-xs text-muted-foreground">
                    {document.title} · {document.page_count} 页
                  </span>
                </span>
              </label>
            ))}
            {!documents.length && (
              <p className="rounded-xl border border-dashed p-4 text-sm text-muted-foreground">
                暂无可检索文档
              </p>
            )}
          </div>
        </CardContent>
        <CardFooter>
          <Button variant="outline" className="w-full" type="button" onClick={onUpload}>
            <UploadCloud data-icon="inline-start" />
            上传新规范
          </Button>
        </CardFooter>
      </Card>

      {activeResult?.citations.length ? (
        <Button type="button" onClick={onOpenEvidence}>
          <PanelRightOpen data-icon="inline-start" />
          打开 {activeResult.citations.length} 条引用资料
        </Button>
      ) : null}
    </div>
  );
}

function LibraryWorkspace({
  documents,
  allDocumentCount,
  search,
  jobs,
  selectedIds,
  dragging,
  uploading,
  fileInputRef,
  onSearch,
  onFileChange,
  onUpload,
  onToggle,
  onDragging,
}: {
  documents: Document[];
  allDocumentCount: number;
  search: string;
  jobs: Record<string, Job>;
  selectedIds: Set<string>;
  dragging: boolean;
  uploading: boolean;
  fileInputRef: RefObject<HTMLInputElement | null>;
  onSearch: (value: string) => void;
  onFileChange: (event: ChangeEvent<HTMLInputElement>) => void;
  onUpload: (file: File) => Promise<void>;
  onToggle: (id: string) => void;
  onDragging: (value: boolean) => void;
}) {
  const onDrop = (event: DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    onDragging(false);
    const file = event.dataTransfer.files[0];
    if (file) void onUpload(file);
  };
  return (
    <>
      <Card className="mb-5 shadow-none">
        <CardContent className="flex flex-col gap-4 p-4 lg:flex-row lg:items-center lg:justify-between">
          <div className="relative w-full max-w-md">
            <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
            <Input
              value={search}
              onChange={(event) => onSearch(event.target.value)}
              placeholder="搜索规范名称、编号或文件名…"
              className="pl-9"
            />
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <Badge variant="secondary">全部 {allDocumentCount}</Badge>
            <Badge variant="outline">PDF ≤ 100 MB</Badge>
            <Button type="button" onClick={() => fileInputRef.current?.click()}>
              <UploadCloud data-icon="inline-start" />
              上传规范
            </Button>
          </div>
        </CardContent>
      </Card>

      <section className="grid gap-4 md:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-4">
        <Card
          className={`min-h-64 border-dashed shadow-none transition ${
            dragging ? "border-primary bg-primary/5" : ""
          }`}
          onDragEnter={(event) => {
            event.preventDefault();
            onDragging(true);
          }}
          onDragOver={(event) => event.preventDefault()}
          onDragLeave={() => onDragging(false)}
          onDrop={onDrop}
        >
          <CardContent className="flex h-full min-h-64 flex-col items-center justify-center p-6 text-center">
            <div className="flex size-12 items-center justify-center rounded-2xl bg-primary/10 text-primary">
              <UploadCloud className="size-6" />
            </div>
            <h3 className="mt-4 font-semibold">{uploading ? "正在上传…" : "添加规范 PDF"}</h3>
            <p className="mt-2 max-w-56 text-sm leading-6 text-muted-foreground">
              拖放到此处，或选择文本型、扫描型规范文件
            </p>
            <Button
              variant="outline"
              className="mt-5"
              type="button"
              disabled={uploading}
              onClick={() => fileInputRef.current?.click()}
            >
              选择文件
            </Button>
            <input
              className="hidden"
              ref={fileInputRef}
              type="file"
              accept=".pdf"
              onChange={onFileChange}
            />
          </CardContent>
        </Card>

        {documents.map((document) => (
          <DocumentCard
            key={document.id}
            document={document}
            job={jobs[document.id]}
            selected={selectedIds.has(document.id)}
            onToggle={() => onToggle(document.id)}
          />
        ))}
      </section>

      {!documents.length && search && (
        <Card className="mt-4 border-dashed shadow-none">
          <CardContent className="flex min-h-40 flex-col items-center justify-center p-6 text-center">
            <FileSearch className="size-7 text-muted-foreground" />
            <p className="mt-3 text-sm font-medium">没有找到匹配的规范</p>
            <p className="mt-1 text-xs text-muted-foreground">尝试使用标准编号或更短的关键词</p>
          </CardContent>
        </Card>
      )}
    </>
  );
}

function DocumentCard({
  document,
  job,
  selected,
  onToggle,
}: {
  document: Document;
  job?: Job;
  selected: boolean;
  onToggle: () => void;
}) {
  const processing = document.status !== "READY" && document.status !== "FAILED";
  return (
    <Card className={`min-h-64 overflow-hidden shadow-none transition hover:shadow-sm ${selected ? "ring-2 ring-primary/30" : ""}`}>
      <CardHeader>
        <div className="flex items-start justify-between gap-3">
          <div className="flex size-10 shrink-0 items-center justify-center rounded-xl bg-muted text-muted-foreground">
            <FileText className="size-5" />
          </div>
          <Badge variant={document.status === "READY" ? "secondary" : "outline"}>
            {STATUS_LABEL[document.status] || document.status}
          </Badge>
        </div>
        <CardTitle className="mt-3 line-clamp-2 text-base">
          {document.standard_no || document.title}
        </CardTitle>
        <CardDescription className="line-clamp-2">{document.title}</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <div className="grid grid-cols-2 gap-2 text-xs text-muted-foreground">
          <span>{document.page_count ? `${document.page_count} 页` : formatBytes(document.file_size)}</span>
          <span className="text-right">{processing ? "正在处理" : "可用于问答"}</span>
          <span>{document.needs_ocr ? "扫描文档" : "电子文档"}</span>
          <span className="truncate text-right">{parserDisplayName(document.parser_name)}</span>
        </div>
        {job && job.status !== "COMPLETED" && (
          <div className="rounded-lg bg-muted/50 p-3">
            <div className="mb-2 flex items-center justify-between gap-2 text-xs">
              <span className="truncate">{job.message}</span>
              <span className="font-medium">{Math.round(job.progress)}%</span>
            </div>
            <Progress value={job.progress} />
          </div>
        )}
        {job?.error && <p className="text-xs text-destructive">{job.error}</p>}
      </CardContent>
      <CardFooter className="mt-auto border-t pt-4">
        <Button
          variant={selected ? "secondary" : "outline"}
          className="w-full"
          type="button"
          disabled={document.status !== "READY"}
          onClick={onToggle}
        >
          {selected ? <Check data-icon="inline-start" /> : <BookOpen data-icon="inline-start" />}
          {selected ? "已加入检索范围" : "加入检索范围"}
        </Button>
      </CardFooter>
    </Card>
  );
}

function EvidenceSheet({
  open,
  onOpenChange,
  result,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  result: ChatResult | null;
}) {
  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent
        side="right"
        className="w-[min(100vw,40rem)] max-w-[100vw] gap-0 overflow-hidden p-0 sm:max-w-[min(100vw,40rem)]"
      >
        <SheetHeader className="min-w-0 border-b p-5 pr-12 text-left">
          <div className="flex min-w-0 flex-wrap items-center gap-2">
            <Badge variant="secondary">{result?.citations.length ?? 0} 条资料</Badge>
            {result && <Badge variant="outline">{QUERY_LABEL[result.query_type] ?? result.query_type}</Badge>}
          </div>
          <SheetTitle className="mt-2">原文资料</SheetTitle>
          <SheetDescription>点击页码可以在新窗口预览 PDF 对应页面。</SheetDescription>
        </SheetHeader>
        <ScrollArea className="h-[calc(100vh-150px)]">
          <div className="flex min-w-0 flex-col gap-4 p-5">
            {!result?.citations.length ? (
              <div className="flex min-h-72 flex-col items-center justify-center text-center">
                <FileSearch className="size-8 text-muted-foreground" />
                <p className="mt-4 text-sm font-medium">暂无引用资料</p>
                <p className="mt-1 max-w-64 text-xs leading-5 text-muted-foreground">
                  完成一次问答后，规范名称、条款号、原文与页码会显示在这里。
                </p>
              </div>
            ) : (
              result.citations.map((citation) => (
                <Card key={citation.chunk_id} className="min-w-0 shadow-none">
                  <CardHeader className="pb-3">
                    <div className="flex items-start justify-between gap-3">
                      <Badge>[{citation.index}]</Badge>
                      <span className="font-mono text-xs text-muted-foreground">
                        {citation.score.toFixed(3)}
                      </span>
                    </div>
                    <CardTitle className="mt-2 break-words text-base">
                      {citation.document_title}
                    </CardTitle>
                    <CardDescription className="break-words">
                      {[citation.standard_no, citation.version ? `${citation.version} 版` : null]
                        .filter(Boolean)
                        .join(" · ")}
                    </CardDescription>
                  </CardHeader>
                  <CardContent>
                    <div className="mb-3 flex flex-wrap gap-2">
                      <Badge variant="outline">
                        {citation.clause_no || citation.chapter_path || "相关内容"}
                      </Badge>
                      <Badge variant="secondary">
                        {citation.source_type.startsWith("normative") ? "规范正文" : "条文说明"}
                      </Badge>
                    </div>
                    <blockquote className="break-words border-l-2 pl-3 text-sm leading-6 text-muted-foreground">
                      {citation.quote}
                    </blockquote>
                  </CardContent>
                  <CardFooter className="border-t pt-4">
                    <Button variant="outline" className="h-auto min-h-9 w-full whitespace-normal" asChild>
                      <a
                        href={`${API_BASE}/api/documents/${citation.document_id}/file#page=${citation.pdf_page}`}
                        target="_blank"
                        rel="noreferrer"
                      >
                        预览 PDF 第 {citation.pdf_page} 页
                        {citation.printed_page ? ` · 纸面第 ${citation.printed_page} 页` : ""}
                        <ArrowUpRight data-icon="inline-end" />
                      </a>
                    </Button>
                  </CardFooter>
                </Card>
              ))
            )}
          </div>
        </ScrollArea>
      </SheetContent>
    </Sheet>
  );
}
