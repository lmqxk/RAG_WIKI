"use client";

import { FormEvent, useState } from "react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { useAuth } from "./AuthContext";

/** 未配置固定地址时，使用访问网页的设备主机名，支持局域网直连。 */
function resolveApiBase(): string {
  const configured = process.env.NEXT_PUBLIC_API_BASE_URL;
  if (configured) return configured;
  if (typeof window === "undefined") return "http://127.0.0.1:8000";
  const port = process.env.NEXT_PUBLIC_API_PORT ?? "8000";
  return `${window.location.protocol}//${window.location.hostname}:${port}`;
}

const API_BASE = resolveApiBase();

type Mode = "login" | "register";

export default function LoginPage({ onSuccess }: { onSuccess: () => void }) {
  const { login } = useAuth();
  const [mode, setMode] = useState<Mode>("login");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [orgId, setOrgId] = useState("default-org");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    setLoading(true);
    try {
      if (mode === "register") {
        const regRes = await fetch(`${API_BASE}/api/auth/register`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ username, password, organization_id: orgId }),
        });
        if (!regRes.ok) {
          const payload = await regRes.json().catch(() => null);
          throw new Error(payload?.detail ?? `注册失败（${regRes.status}）`);
        }
      }
      const loginRes = await fetch(`${API_BASE}/api/auth/login`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username, password }),
      });
      if (!loginRes.ok) {
        const payload = await loginRes.json().catch(() => null);
        throw new Error(payload?.detail ?? `登录失败（${loginRes.status}）`);
      }
      const data = await loginRes.json();
      login(data.access_token, orgId);
      onSuccess();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-gradient-to-br from-slate-50 to-slate-100 p-4">
      <Card className="w-full max-w-md shadow-lg">
        <CardHeader className="text-center">
          <CardTitle className="text-2xl">规智库</CardTitle>
          <CardDescription>
            {mode === "login" ? "登录以继续使用" : "注册新账号"}
          </CardDescription>
        </CardHeader>
        <CardContent>
          <form onSubmit={handleSubmit} className="space-y-4">
            {error && (
              <Alert variant="destructive">
                <AlertDescription>{error}</AlertDescription>
              </Alert>
            )}
            <div className="space-y-2">
              <label className="text-sm font-medium">用户名</label>
              <Input
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                placeholder="输入用户名"
                required
                minLength={3}
              />
            </div>
            <div className="space-y-2">
              <label className="text-sm font-medium">密码</label>
              <Input
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                placeholder="输入密码"
                required
                minLength={8}
              />
            </div>
            {mode === "register" && (
              <div className="space-y-2">
                <label className="text-sm font-medium">组织 ID（可选）</label>
                <Input
                  value={orgId}
                  onChange={(e) => setOrgId(e.target.value || "default-org")}
                  placeholder="default-org"
                />
              </div>
            )}
            <Button type="submit" className="w-full" disabled={loading}>
              {loading ? "处理中..." : mode === "login" ? "登录" : "注册并登录"}
            </Button>
            <div className="text-center text-sm text-muted-foreground">
              {mode === "login" ? (
                <span>
                  还没有账号？{" "}
                  <button
                    type="button"
                    className="text-primary underline hover:no-underline"
                    onClick={() => { setMode("register"); setError(null); }}
                  >
                    注册
                  </button>
                </span>
              ) : (
                <span>
                  已有账号？{" "}
                  <button
                    type="button"
                    className="text-primary underline hover:no-underline"
                    onClick={() => { setMode("login"); setError(null); }}
                  >
                    登录
                  </button>
                </span>
              )}
            </div>
          </form>
        </CardContent>
      </Card>
    </div>
  );
}