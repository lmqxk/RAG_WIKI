import type { Metadata } from "next";
import { TooltipProvider } from "@/components/ui/tooltip";
import { AuthProvider } from "./AuthContext";
import "./globals.css";

export const metadata: Metadata = {
  metadataBase: new URL(process.env.NEXT_PUBLIC_SITE_URL ?? "http://localhost:3000"),
  title: "规智库 · 规范智能问答工作台",
  description: "支持规范 PDF 管理、条款问答、跨文档对比与原文引用的智能问答系统。",
  openGraph: {
    title: "规智库 · 规范智能问答工作台",
    description: "规范 PDF 管理、条款问答、跨文档对比与原文引用。",
    images: [{ url: "/og.png", width: 1672, height: 939, alt: "规智库智能问答工作台" }],
  },
  twitter: {
    card: "summary_large_image",
    title: "规智库 · 规范智能问答工作台",
    description: "规范 PDF 管理、条款问答、跨文档对比与原文引用。",
    images: ["/og.png"],
  },
  icons: {
    icon: "/favicon.svg",
    shortcut: "/favicon.svg",
  },
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="zh-CN">
      <body>
        <TooltipProvider>
          <AuthProvider>{children}</AuthProvider>
        </TooltipProvider>
      </body>
    </html>
  );
}
