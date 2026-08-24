"use client";

import { createContext, useCallback, useContext, useEffect, useState } from "react";

const STORAGE_KEY_TOKEN = "rag_zb_auth_token";
const STORAGE_KEY_ORG = "rag_zb_org_id";

type AuthState = {
  token: string | null;
  orgId: string | null;
  isAuthenticated: boolean;
};

type AuthContextType = AuthState & {
  login: (token: string, orgId?: string) => void;
  logout: () => void;
  setOrgId: (orgId: string) => void;
};

const AuthContext = createContext<AuthContextType | null>(null);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [state, setState] = useState<AuthState>(() => {
    if (typeof window === "undefined") return { token: null, orgId: null, isAuthenticated: false };
    return {
      token: localStorage.getItem(STORAGE_KEY_TOKEN),
      orgId: localStorage.getItem(STORAGE_KEY_ORG) || "default-org",
      isAuthenticated: !!localStorage.getItem(STORAGE_KEY_TOKEN),
    };
  });

  const login = useCallback((token: string, orgId?: string) => {
    localStorage.setItem(STORAGE_KEY_TOKEN, token);
    const oid = orgId || "default-org";
    localStorage.setItem(STORAGE_KEY_ORG, oid);
    setState({ token, orgId: oid, isAuthenticated: true });
  }, []);

  const logout = useCallback(() => {
    localStorage.removeItem(STORAGE_KEY_TOKEN);
    localStorage.removeItem(STORAGE_KEY_ORG);
    setState({ token: null, orgId: null, isAuthenticated: false });
  }, []);

  const setOrgId = useCallback((orgId: string) => {
    localStorage.setItem(STORAGE_KEY_ORG, orgId);
    setState((prev) => ({ ...prev, orgId }));
  }, []);

  // 同步 localStorage 变更（多标签页）
  useEffect(() => {
    const handler = (e: StorageEvent) => {
      if (e.key === STORAGE_KEY_TOKEN) {
        setState((prev) => ({
          ...prev,
          token: e.newValue,
          isAuthenticated: !!e.newValue,
        }));
      }
      if (e.key === STORAGE_KEY_ORG) {
        setState((prev) => ({ ...prev, orgId: e.newValue || "default-org" }));
      }
    };
    window.addEventListener("storage", handler);
    return () => window.removeEventListener("storage", handler);
  }, []);

  return (
    <AuthContext.Provider value={{ ...state, login, logout, setOrgId }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth(): AuthContextType {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within AuthProvider");
  return ctx;
}

/** 返回带认证头的 fetch 参数。 */
export function authHeaders(token: string | null): Record<string, string> {
  const headers: Record<string, string> = {};
  if (token) headers["Authorization"] = `Bearer ${token}`;
  return headers;
}

/** 返回带认证 + 组织的 fetch 参数。 */
export function authOrgHeaders(token: string | null, orgId: string | null): Record<string, string> {
  const headers = authHeaders(token);
  if (orgId) headers["X-Organization-ID"] = orgId;
  return headers;
}