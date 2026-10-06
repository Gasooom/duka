"use client";

export class ApiError extends Error {
  status: number;
  data: any;
  constructor(status: number, message: string, data: any) {
    super(message);
    this.status = status;
    this.data = data;
  }
}

const TOKEN_KEY = "duka_token";

export function getToken(): string | null {
  try {
    return localStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

export function setToken(token: string | null) {
  try {
    if (token) localStorage.setItem(TOKEN_KEY, token);
    else localStorage.removeItem(TOKEN_KEY);
  } catch {}
}

type ApiOptions = { method?: string; body?: any; form?: FormData };
const inFlight = new Map<string, Promise<any>>();

/** API call. A double click / double tap must never create two products, payments or WhatsApp messages: while
 * an identical write is still in flight, a repeat of it gets the first request's result instead of a second
 * request (React re-renders a disabled button too late to stop a fast second click). */
export function api<T = any>(path: string, opts: ApiOptions = {}): Promise<T> {
  const method = opts.method || (opts.form || opts.body !== undefined ? "POST" : "GET");
  if (method === "GET") return request<T>(path, opts);
  const key = `${method} ${path} ${opts.form ? "form" : JSON.stringify(opts.body ?? null)}`;
  const running = inFlight.get(key);
  if (running) return running as Promise<T>;
  const p = request<T>(path, opts).finally(() => inFlight.delete(key));
  inFlight.set(key, p);
  return p;
}

async function request<T>(path: string, opts: ApiOptions): Promise<T> {
  const headers: Record<string, string> = {};
  const token = getToken();
  if (token) headers["Authorization"] = `Bearer ${token}`;
  let body: BodyInit | undefined;
  if (opts.form) body = opts.form;
  else if (opts.body !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(opts.body);
  }
  const res = await fetch(`/api${path}`, { method: opts.method || (body ? "POST" : "GET"), headers, body });
  if (res.status === 401 && typeof window !== "undefined" && !path.startsWith("/auth/")) {
    setToken(null);
    window.location.href = "/login";
  }
  if (res.status === 204) return undefined as T;
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const msg = data?.errors?.length
      ? data.errors.map((e: any) => `${e.field}: ${e.message}`).join("; ")
      : data?.detail || `Request failed (${res.status})`;
    throw new ApiError(res.status, typeof msg === "string" ? msg : JSON.stringify(msg), data);
  }
  return data as T;
}

export function money(amount: number | null | undefined, currency = "RWF") {
  if (amount === null || amount === undefined) return "—";
  return `${currency} ${Number(amount).toLocaleString("en-US", { maximumFractionDigits: 2 })}`;
}

export function when(iso: string | null | undefined) {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleString("en-GB", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" });
}
