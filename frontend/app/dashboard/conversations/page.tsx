"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense } from "react";
import { when } from "@/lib/api";
import { Badge, Empty, ErrorNote, LanguageTag, PageHeader, useApi } from "@/components/ui";

function Inner() {
  const params = useSearchParams();
  const router = useRouter();
  const attention = params.get("attention") === "1";
  const { data, error } = useApi<any[]>(`/conversations${attention ? "?needs_attention=true" : ""}`, 10000);
  return (
    <div>
      <PageHeader title="Conversations" subtitle="Open one to see the full AI trace: decisions, tool calls, inputs, outputs, latency, tokens." />
      <ErrorNote error={error} />
      <div className="mb-3 flex gap-1.5">
        {[["All", false], ["Needs human attention", true]].map(([label, v]) => (
          <button key={String(label)} onClick={() => router.replace(`/dashboard/conversations${v ? "?attention=1" : ""}`)}
            className={`rounded-full border px-2.5 py-1 text-xs ${attention === v ? "border-brand bg-brand-soft text-brand" : "border-line bg-white text-ink-soft"}`}>
            {label as string}
          </button>
        ))}
      </div>
      <div className="card">
        {data && data.length === 0 ? <Empty>No conversations{attention ? " need attention" : " yet"}.</Empty> : (
          <ul>
            {data?.map((c) => (
              <li key={c.id} className="border-b border-line last:border-0">
                <Link href={`/dashboard/conversations/${c.id}`} className="flex items-center gap-3 px-4 py-3 hover:bg-canvas">
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2">
                      <span className="font-medium">{c.customer.name || "Customer"}</span>
                      <span className="font-mono text-xs text-ink-mute">+{c.customer.whatsapp_number}</span>
                      <Badge value={c.status} />
                      <LanguageTag code={c.language} />
                      {c.needs_attention && <span className="rounded bg-amber-100 px-1.5 py-0.5 text-xs font-medium text-amber-900">needs attention</span>}
                    </div>
                    <div className="mt-0.5 truncate text-sm text-ink-mute">
                      {c.last_message ? `${c.last_message.role === "customer" ? "" : "↳ "}${c.last_message.content}` : ""}
                    </div>
                  </div>
                  <div className="shrink-0 text-xs text-ink-mute">{when(c.last_message_at)}</div>
                </Link>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

export default function Conversations() {
  return <Suspense><Inner /></Suspense>;
}
