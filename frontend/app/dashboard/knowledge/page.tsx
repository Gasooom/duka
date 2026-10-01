"use client";

import { FormEvent, useState } from "react";
import { api, when } from "@/lib/api";
import { Empty, ErrorNote, Field, OkNote, PageHeader, useApi } from "@/components/ui";

export default function Knowledge() {
  const { data, error, reload } = useApi<any[]>("/knowledge");
  const [err, setErr] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [q, setQ] = useState("");
  const [hits, setHits] = useState<any[] | null>(null);

  async function act(fn: () => Promise<any>, ok: string) {
    setErr(null); setMsg(null);
    try { await fn(); setMsg(ok); reload(); } catch (e: any) { setErr(e.message); }
  }
  function addText(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const form = e.currentTarget;
    const f = Object.fromEntries(new FormData(form));
    act(async () => { await api("/knowledge", { body: f }); form.reset(); }, "Document added and indexed.");
  }
  function upload(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const form = e.currentTarget;
    act(async () => { await api("/knowledge/upload", { form: new FormData(form) }); form.reset(); }, "File uploaded and indexed.");
  }

  return (
    <div className="max-w-4xl">
      <PageHeader title="Knowledge base" subtitle="FAQs and policies the assistant can quote (delivery, returns, warranty, hours). Products, prices and orders always come from live data, not from here." />
      <ErrorNote error={error || err} />
      <OkNote text={msg} />
      <div className="grid gap-4 md:grid-cols-2">
        <form onSubmit={addText} className="card space-y-3 p-4">
          <h2 className="text-sm font-semibold">Add text</h2>
          <Field label="Title"><input name="title" required className="input" placeholder="Return policy" /></Field>
          <Field label="Content"><textarea name="content" rows={5} required className="input" /></Field>
          <button className="btn-primary">Add</button>
        </form>
        <div className="space-y-4">
          <form onSubmit={upload} className="card space-y-3 p-4">
            <h2 className="text-sm font-semibold">Upload PDF / TXT / MD</h2>
            <Field label="Title (optional)"><input name="title" className="input" /></Field>
            <input type="file" name="file" accept=".pdf,.txt,.md" required className="text-sm" />
            <div><button className="btn-primary">Upload</button></div>
          </form>
          <form onSubmit={async (e) => { e.preventDefault(); try { setHits(await api(`/knowledge/search?q=${encodeURIComponent(q)}`)); } catch (er: any) { setErr(er.message); } }} className="card space-y-2 p-4">
            <h2 className="text-sm font-semibold">Test retrieval</h2>
            <div className="flex gap-2"><input className="input" value={q} onChange={(e) => setQ(e.target.value)} placeholder="Do you deliver outside Kigali?" required /><button className="btn-ghost">Search</button></div>
            {hits && (hits.length === 0 ? <p className="text-xs text-ink-mute">No relevant chunks — the assistant will say it isn't sure.</p> :
              hits.map((h, i) => <div key={i} className="rounded border border-line p-2 text-xs"><b>{h.document_title}</b> <span className="text-ink-mute">score {h.score}</span><div className="mt-1">{h.content}</div></div>))}
          </form>
        </div>
      </div>
      <div className="card mt-5">
        {data && data.length === 0 ? <Empty>No documents yet.</Empty> : (
          <table className="w-full">
            <thead><tr><th className="th">Title</th><th className="th">Source</th><th className="th text-right">Chunks</th><th className="th text-right">Added</th><th className="th"></th></tr></thead>
            <tbody>
              {data?.map((d) => (
                <tr key={d.id}>
                  <td className="td"><div className="font-medium">{d.title}</div><div className="max-w-md truncate text-xs text-ink-mute">{d.content}</div></td>
                  <td className="td">{d.source_type}</td>
                  <td className="td text-right">{d.chunk_count}</td>
                  <td className="td text-right text-ink-mute">{when(d.created_at)}</td>
                  <td className="td text-right"><button className="text-xs text-red-700" onClick={() => confirm("Delete document?") && act(() => api(`/knowledge/${d.id}`, { method: "DELETE" }), "Deleted.")}>Delete</button></td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
