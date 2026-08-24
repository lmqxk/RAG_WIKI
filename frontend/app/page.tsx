"use client";

import { useEffect, useState } from "react";
import { useAuth } from "./AuthContext";
import LoginPage from "./LoginPage";
import { RagDashboard } from "./RagDashboard.tsx";

function resolveApiBase(): string {
  const configured = process.env.NEXT_PUBLIC_API_BASE_URL;
  if (configured) return configured;
  if (typeof window === "undefined") return "http://127.0.0.1:8000";
  const port = process.env.NEXT_PUBLIC_API_PORT ?? "8000";
  return `${window.location.protocol}//${window.location.hostname}:${port}`;
}

const API_BASE = resolveApiBase();

export default function Home() {
  const { isAuthenticated, token } = useAuth();
  const [authEnabled, setAuthEnabled] = useState<boolean | null>(null);
  const [ready, setReady] = useState(false);

  useEffect(() => {
    fetch(`${API_BASE}/api/health`)
      .then((res) => res.json())
      .then((data) => {
        // health 端点返回 auth_enabled 字段，直接判断
        // 如果有 token 说明已登录，直接进
        if (token || !data.auth_enabled) {
          setAuthEnabled(data.auth_enabled);
          setReady(true);
        } else {
          setAuthEnabled(true);
          setReady(true);
        }
      })
      .catch(() => {
        // 后端不可达，尝试直接进
        setAuthEnabled(false);
        setReady(true);
      });
  }, [token]);

  if (!ready) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-gradient-to-br from-slate-50 to-slate-100">
        <p className="text-muted-foreground">正在连接服务...</p>
      </div>
    );
  }

  if (authEnabled && !isAuthenticated) {
    return <LoginPage onSuccess={() => setReady(true)} />;
  }

  return <RagDashboard />;
}
