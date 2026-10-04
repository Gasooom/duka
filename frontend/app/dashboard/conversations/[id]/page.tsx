"use client";

import Link from "next/link";
import { use, useState } from "react";
import { api, when } from "@/lib/api";
import { Badge, ErrorNote, useApi } from "@/components/ui";

function Json({ value }: { value: any }) {
  return <pre className="max-h-64 overflow-auto rounded bg-canvas p-2 font-mono text-[11px] leading-relaxed">{JSON.stringify(value, null, 2)}</pre>;
}

function RunTrace({ run }: { run: any }) {
  const [open, setOpen] = useState(false);
  const tools = run.steps.filter((s: any) => s.type === "tool");
  return (
    <div className="rounded-md border border-line bg-white">
      <button onClick={() => setOpen(!open)} className="flex w-full flex-wrap items-center gap-2 px-3 py-2 text-left text-xs">
        <Badge value={run.status} />
        <span className="font-mono">{run.provider}{run.model ? ` · ${run.model}` : ""}</span>
        <span className="text-ink-mute">{run.latency_ms} ms</span>
        <span className="text-ink-mute">{run.provider === "rules" ? "no LLM (rules engine)" : `${run.llm_calls} LLM call(s)`}</span>
        {(run.prompt_tokens || run.completion_tokens) && <span className="text-ink-mute">{run.prompt_tokens ?? 0} in / {run.completion_tokens ?? 0} out tokens</span>}
        <span className="flex-1 truncate text-ink-soft">{tools.map((t: any) => t.tool).join(" → ") || "no tools"}</span>
        <span className="text-ink-mute">{open ? "hide" : "trace"}</span>
      </button>
      {open && (
        <div className="space-y-2 border-t border-line p-3 text-xs">
          <div><span className="text-ink-mute">Input:</span> {run.input_text}</div>
          {run.steps.map((s: any, i: number) => (
            <div key={i} className="rounded border border-line p-2">
              {s.type === "llm" && (
                <div>
                  <b>{run.provider === "rules" ? "Rules decision" : "LLM decision"}:</b> {s.decision} <span className="text-ink-mute">· {s.latency_ms} ms{s.prompt_tokens ? ` · ${s.prompt_tokens}/${s.completion_tokens} tokens` : ""}</span>
                  {s.content && <div className="mt-1 whitespace-pre-wrap text-ink-soft">{s.content}</div>}
                </div>
              )}
              {s.type === "tool" && (
                <div>
                  <div className="mb-1 flex items-center gap-2">
                    <b className="font-mono">{s.tool}</b>
                    <Badge value={s.ok ? "success" : "error"} />
                    <span className="text-ink-mute">{s.latency_ms} ms</span>
                  </div>
                  {s.error && <div className="mb-1 text-red-700">{s.error}</div>}
                  <div className="grid gap-2 md:grid-cols-2">
                    <div><div className="mb-0.5 text-ink-mute">arguments</div><Json value={s.arguments} /></div>
                    <div><div className="mb-0.5 text-ink-mute">result</div><Json value={s.result} /></div>
                  </div>
                </div>
              )}
              {s.type === "fast_path" && <div><b>Fast path:</b> {s.reason} (no LLM call)</div>}
              {s.type === "error" && <div className="text-red-700"><b>Error:</b> {s.error}</div>}
            </div>
          ))}
          {run.error && <div className="text-red-700">Run error: {run.error}</div>}
        </div>
      )}
    </div>
  );
}

const ROLE_STYLE: Record<string, string> = {
  customer: "mr-auto bg-white border border-line",
  assistant: "ml-auto bg-brand-soft",
  human_agent: "ml-auto bg-amber-50 border border-amber-200",
};

export default function ConversationDebugger({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const { data: c, error, reload } = useApi<any>(`/conversations/${id}`);
  const [showTools, setShowTools] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [reply, setReply] = useState("");

  async function act(fn: () => Promise<any>) {
    setErr(null);
    try { await fn(); await reload(); } catch (e: any) { setErr(e.message); }
  }
  if (!c) return <ErrorNote error={error} />;
  const runs: Record<string, any> = Object.fromEntries(c.agent_runs.map((r: any) => [r.trigger_message_id, r]));
  const visible = c.messages.filter((m: any) => showTools || !["tool_call", "tool_result"].includes(m.role));

  return (
    <div>
      <Link href="/dashboard/conversations" className="text-xs text-brand">← Conversations</Link>
      <div className="mb-4 mt-2 flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="flex items-center gap-2 text-xl font-semibold">{c.customer.name || "Customer"} <Badge value={c.status} /></h1>
          <div className="font-mono text-xs text-ink-mute">+{c.customer.whatsapp_number} · conversation {c.id.slice(0, 8)}</div>
          {c.handoff_reason && <div className="mt-1 text-xs text-amber-800">Handoff: {c.handoff_reason}</div>}
        </div>
        <div className="flex gap-2">
          <label className="flex items-center gap-1.5 text-xs text-ink-soft"><input type="checkbox" checked={showTools} onChange={(e) => setShowTools(e.target.checked)} /> show tool messages</label>
          <button className="btn-ghost" onClick={reload}>Refresh</button>
          {c.status === "ai"
            ? <button className="btn-ghost" onClick={() => act(() => api(`/conversations/${id}/handoff`, { method: "POST" }))}>Take over</button>
            : <button className="btn-primary" onClick={() => {
                const note = prompt("Optional message to the customer before the assistant takes over again (leave empty for none):", "");
                if (note === null) return;
                act(() => api(`/conversations/${id}/return-to-ai`, { body: { message: note || null } }));
              }}>Return to AI</button>}
        </div>
      </div>
      <ErrorNote error={err} />
      {c.summary && <div className="card mb-3 p-3 text-xs"><b>Rolling summary:</b> {c.summary}</div>}
      <div className="space-y-3">
        {visible.map((m: any) => (
          <div key={m.id}>
            {["tool_call", "tool_result", "system"].includes(m.role) ? (
              <div className="mx-auto max-w-3xl rounded border border-dashed border-line px-2 py-1 font-mono text-[11px] text-ink-mute">
                [{m.role}] {m.content.slice(0, 400)}
              </div>
            ) : (
              <div className={`max-w-xl rounded-lg px-3 py-2 ${ROLE_STYLE[m.role] || ""}`}>
                <div className="whitespace-pre-wrap text-sm">{m.content}</div>
                <div className="mt-1 flex items-center gap-2 text-[11px] text-ink-mute">
                  <span>{m.role === "human_agent" ? "staff" : m.role}</span><span>{when(m.created_at)}</span>
                  {m.role !== "customer" && <Badge value={m.delivery_status} />}
                  {m.metadata?.error && <span className="text-red-700">{m.metadata.error}</span>}
                </div>
              </div>
            )}
            {m.role === "customer" && runs[m.id] && <div className="mt-2 max-w-3xl"><RunTrace run={runs[m.id]} /></div>}
          </div>
        ))}
      </div>
      {c.status === "human" && (
        <form className="card mt-5 flex gap-2 p-3" onSubmit={(e) => { e.preventDefault(); act(async () => { await api(`/conversations/${id}/reply`, { body: { text: reply } }); setReply(""); }); }}>
          <input className="input" placeholder="Reply as staff (sent via WhatsApp)…" value={reply} onChange={(e) => setReply(e.target.value)} required />
          <button className="btn-primary">Send</button>
        </form>
      )}
    </div>
  );
}
