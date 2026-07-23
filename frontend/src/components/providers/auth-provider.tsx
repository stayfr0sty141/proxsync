"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { api } from "@/lib/api/client";
import { tokenStore } from "@/lib/api/token-store";
import type { LoginRequest, TokenResponse, UserResponse } from "@/types/api";

/**
 * Auth state for the whole app.
 *
 * On mount it attempts one silent refresh: the access token lives only in memory
 * (docs/ARCHITECTURE.md), so a page reload has none, but the HttpOnly refresh
 * cookie survives. If the refresh succeeds we fetch `/auth/me` and land the user
 * back where they were; if it fails we render the login screen. `login` and
 * `logout` keep the in-memory token and the React state in lock-step.
 */

interface AuthContextValue {
  user: UserResponse | null;
  status: "loading" | "authenticated" | "unauthenticated";
  login: (credentials: LoginRequest) => Promise<UserResponse>;
  logout: () => Promise<void>;
  refreshUser: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<UserResponse | null>(null);
  const [status, setStatus] = useState<AuthContextValue["status"]>("loading");

  const bootstrap = useCallback(async () => {
    try {
      // Try to mint an access token from the refresh cookie. skipAuthRetry so a
      // 401 here does not recurse into another refresh.
      const refreshed = await api.post<{ access_token: string; csrf_token: string }>(
        "/auth/refresh",
        undefined,
        { skipAuthRetry: true },
      );
      tokenStore.set(refreshed.access_token, refreshed.csrf_token);
      const me = await api.get<UserResponse>("/auth/me");
      setUser(me);
      setStatus("authenticated");
    } catch {
      tokenStore.clear();
      setUser(null);
      setStatus("unauthenticated");
    }
  }, []);

  useEffect(() => {
    void bootstrap();
  }, [bootstrap]);

  const login = useCallback(async (credentials: LoginRequest) => {
    const response = await api.post<TokenResponse>("/auth/login", credentials, {
      skipAuthRetry: true,
    });
    tokenStore.set(response.access_token, response.csrf_token);
    setUser(response.user);
    setStatus("authenticated");
    return response.user;
  }, []);

  const logout = useCallback(async () => {
    try {
      await api.post("/auth/logout");
    } catch {
      // Even if the server call fails, drop local auth so the UI locks.
    } finally {
      tokenStore.clear();
      setUser(null);
      setStatus("unauthenticated");
    }
  }, []);

  const refreshUser = useCallback(async () => {
    const me = await api.get<UserResponse>("/auth/me");
    setUser(me);
  }, []);

  const value = useMemo<AuthContextValue>(
    () => ({ user, status, login, logout, refreshUser }),
    [user, status, login, logout, refreshUser],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const context = useContext(AuthContext);
  if (!context) {
    throw new Error("useAuth must be used within an AuthProvider");
  }
  return context;
}

/** Role hierarchy helper: admin > operator > viewer. */
const ROLE_RANK: Record<string, number> = { viewer: 0, operator: 1, admin: 2 };

export function hasRole(
  user: UserResponse | null,
  required: "viewer" | "operator" | "admin",
): boolean {
  if (!user) return false;
  return (ROLE_RANK[user.role] ?? -1) >= (ROLE_RANK[required] ?? 0);
}
