"use client";

import { ReactNode, useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";

export function PageHeader({ title, subtitle, actions }: { title: string; subtitle?: string; actions?: ReactNode }) {
  return (
    <div className="mb-5 flex flex-wrap items-end justify-between gap-3">
      <div>
        <h1 className="text-xl font-semibold">{title}</h1>
        {subtitle && <p className="mt-0.5 text-sm text-ink-mute">{subtitle}</p>}
      </div>
      {actions && <div className="flex gap-2">{actions}</div>}
    </div>
  );
}

const STATUS_STYLES: Record<string, string> = {
  pending: "bg-amber-50 text-amber-800",
  accepted: "bg-sky-50 text-sky-800",
  unpaid: "bg-gray-100 text-gray-700",
  paid: "bg-emerald-50 text-emerald-800",
  voided: "bg-gray-100 text-gray-500",
  queued: "bg-gray-100 text-gray-700",
  retry: "bg-amber-50 text-amber-800",
  skipped: "bg-gray-100 text-gray-500",
  handoff: "bg-amber-50 text-amber-800",
  order_confirmed: "bg-emerald-50 text-emerald-800",
  ready: "bg-indigo-50 text-indigo-800",
  out_for_delivery: "bg-violet-50 text-violet-800",
  delivered: "bg-emerald-100 text-emerald-900",
  cancelled: "bg-red-50 text-red-700",
  successful: "bg-emerald-50 text-emerald-800",
  failed: "bg-red-50 text-red-700",
  ai: "bg-brand-soft text-brand",
  human: "bg-amber-50 text-amber-800",
  success: "bg-emerald-50 text-emerald-800",
  error: "bg-red-50 text-red-700",
  fast_path: "bg-gray-100 text-gray-700",
  sent: "bg-emerald-50 text-emerald-800",
  simulated: "bg-gray-100 text-gray-700",
  received: "bg-gray-100 text-gray-700",
  active: "bg-emerald-50 text-emerald-800",
  inactive: "bg-gray-100 text-gray-500",
};

const LANGUAGES: Record<string, string> = {
  en: "English", rw: "Kinyarwanda", fr: "Français", sw: "Kiswahili", ar: "Arabic", "ar-SD": "Sudanese Arabic",
};

/** The language the customer is writing in (detected; drives the assistant and every automatic message). */
export function LanguageTag({ code }: { code: string | null | undefined }) {
  if (!code) return null;
  return <span className="inline-block whitespace-nowrap rounded border border-line px-1.5 py-0.5 text-xs text-ink-soft" title="Conversation language">{LANGUAGES[code] || code}</span>;
}

export function Badge({ value }: { value: string | null | undefined }) {
  if (!value) return null;
  return (
    <span className={`inline-block whitespace-nowrap rounded px-1.5 py-0.5 text-xs font-medium ${STATUS_STYLES[value] || "bg-gray-100 text-gray-700"}`}>
      {value.replace(/_/g, " ")}
    </span>
  );
}

export function ErrorNote({ error }: { error: string | null }) {
  if (!error) return null;
  return <div className="mb-3 rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-800">{error}</div>;
}

export function OkNote({ text }: { text: string | null }) {
  if (!text) return null;
  return <div className="mb-3 rounded-md border border-emerald-200 bg-emerald-50 px-3 py-2 text-sm text-emerald-800">{text}</div>;
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="px-4 py-10 text-center text-sm text-ink-mute">{children}</div>;
}

export function Field({ label, children, hint }: { label: string; children: ReactNode; hint?: string }) {
  return (
    <label className="block">
      <span className="label">{label}</span>
      {children}
      {hint && <span className="mt-1 block text-xs text-ink-mute">{hint}</span>}
    </label>
  );
}

/** Fetch helper: returns data, error, loading and a reload function. With `refreshMs` it re-fetches on that
 * interval while the tab is visible, so new orders and handoffs show up without reloading the page. */
export function useApi<T = any>(path: string | null, refreshMs?: number) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const reload = useCallback(async () => {
    if (!path) return;
    setLoading(true);
    try {
      setData(await api<T>(path));
      setError(null);
    } catch (e: any) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }, [path]);
  useEffect(() => {
    reload();
  }, [reload]);
  useEffect(() => {
    if (!refreshMs) return;
    const id = setInterval(() => { if (document.visibilityState === "visible") reload(); }, refreshMs);
    return () => clearInterval(id);
  }, [reload, refreshMs]);
  return { data, error, loading, reload, setData };
}
