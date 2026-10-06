"use client";

import Link from "next/link";
import { FormEvent, useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import { Badge, Empty, ErrorNote, Field, NotLoaded, OkNote, PageHeader, useApi } from "@/components/ui";

type Bubble = { role: string; content: string; run?: string | null; status?: string };

function Simulator() {
  const [from, setFrom] = useState("250788555666");
  const [text, setText] = useState("");
  const [chat, setChat] = useState<Bubble[]>([]);
  const [busy, setBusy] = useState(false);
  const [conv, setConv] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const end = useRef<HTMLDivElement>(null);
  useEffect(() => end.current?.scrollIntoView({ block: "nearest" }), [chat]);

  async function send(e: FormEvent) {
    e.preventDefault();
    if (!text.trim()) return;
    const msg = text;
    setText(""); setBusy(true); setErr(null);
    setChat((c) => [...c, { role: "customer", content: msg }]);
    try {
      const r = await api("/dev/simulate", { body: { text: msg, from_number: from, name: "Simulator" } });
      setConv(r.conversation_id);
      setChat((c) => [...c, r.reply ? { role: "assistant", content: r.reply, run: r.agent_run_id, status: r.status }
        : { role: "system", content: `No reply (${r.status})${r.status === "human_mode" ? " — conversation is with a human" : ""}` }]);
    } catch (e: any) {
      setErr(e.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="card flex h-[560px] flex-col">
      <div className="flex flex-wrap items-center gap-2 border-b border-line px-4 py-3">
        <h2 className="text-sm font-semibold">Customer simulator</h2>
        <span className="text-xs text-ink-mute">Runs the real webhook pipeline; replies go through the dev adapter (no WhatsApp cost).</span>
        <div className="ml-auto flex items-center gap-2">
          <span className="whitespace-nowrap text-xs text-ink-mute">from +</span>
          <input value={from} onChange={(e) => setFrom(e.target.value.replace(/\D/g, ""))} className="input w-36 py-1 font-mono text-xs" />
          <button className="btn-ghost whitespace-nowrap py-1 text-xs" onClick={() => { setChat([]); setFrom(`2507${Math.floor(10000000 + Math.random() * 89999999)}`); }}>New customer</button>
        </div>
      </div>
      <div className="flex-1 space-y-2 overflow-y-auto bg-canvas p-4">
        {chat.length === 0 && <Empty>Try: “Hi, I'm looking for black sneakers under 100,000 RWF.”</Empty>}
        {chat.map((b, i) => (
          <div key={i} className={`max-w-[80%] whitespace-pre-wrap rounded-lg px-3 py-2 text-sm ${b.role === "customer" ? "ml-auto bg-[#dcf8c6]" : b.role === "system" ? "mx-auto bg-white text-xs text-ink-mute" : "bg-white"}`}>
            {b.content}
          </div>
        ))}
        <div ref={end} />
      </div>
      <ErrorNote error={err} />
      <form onSubmit={send} className="flex gap-2 border-t border-line p-3">
        <input className="input" value={text} onChange={(e) => setText(e.target.value)} placeholder="Type as the customer…" disabled={busy} />
        <button className="btn-primary" disabled={busy}>{busy ? "…" : "Send"}</button>
        {conv && <Link href={`/dashboard/conversations/${conv}`} className="btn-ghost whitespace-nowrap">Debug</Link>}
      </form>
    </section>
  );
}

export default function WhatsAppPage() {
  const { data: accounts, error, reload, loading } = useApi<any[]>("/whatsapp/accounts");
  const { data: me } = useApi<any>("/auth/me");
  const [err, setErr] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [origin, setOrigin] = useState("https://your-backend");
  useEffect(() => setOrigin(window.location.origin.replace(":3000", ":8000")), []);

  async function connect(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const form = e.currentTarget;
    const f = Object.fromEntries(new FormData(form)) as Record<string, any>;
    for (const k of Object.keys(f)) if (f[k] === "") delete f[k];
    setErr(null); setMsg(null);
    try { await api("/whatsapp/accounts", { body: f }); setMsg("WhatsApp number connected."); form.reset(); reload(); }
    catch (er: any) { setErr(er.message); }
  }

  return (
    <div>
      <PageHeader title="WhatsApp" />
      <ErrorNote error={error || err} />
      <OkNote text={msg} />
      <div className="grid gap-5 xl:grid-cols-2">
        <div className="space-y-5">
          <section className="card">
            <div className="border-b border-line px-4 py-3"><h2 className="text-sm font-semibold">Connected numbers</h2></div>
            {!accounts ? <NotLoaded loading={loading} /> : accounts.length === 0 ? <Empty>No number connected. The simulator will create a dev number automatically.</Empty> : (
              <table className="w-full"><tbody>
                {accounts?.map((a) => (
                  <tr key={a.id}>
                    <td className="td"><div className="font-medium">{a.display_phone_number || "—"}</div><div className="font-mono text-xs text-ink-mute">phone_number_id {a.phone_number_id}</div></td>
                    <td className="td"><Badge value={a.mode === "cloud" ? "sent" : "simulated"} /> <span className="text-xs text-ink-mute">{a.mode}</span></td>
                    <td className="td text-xs">{a.mode === "cloud" ? (a.has_access_token ? "token stored (encrypted)" : <span className="text-red-700">token missing</span>) : "no token needed"}</td>
                    <td className="td text-right"><button className="text-xs text-red-700" onClick={async () => { if (confirm("Disconnect this number?")) { await api(`/whatsapp/accounts/${a.id}`, { method: "DELETE" }); reload(); } }}>Disconnect</button></td>
                  </tr>
                ))}
              </tbody></table>
            )}
          </section>
          <form onSubmit={connect} className="card grid gap-3 p-4 md:grid-cols-2">
            <h2 className="text-sm font-semibold md:col-span-2">Connect WhatsApp Cloud API number</h2>
            <Field label="Phone number ID" hint="Meta → WhatsApp → API Setup"><input name="phone_number_id" required className="input font-mono" /></Field>
            <Field label="Display number"><input name="display_phone_number" className="input" placeholder="+250 7xx xxx xxx" /></Field>
            <Field label="WhatsApp Business Account ID"><input name="waba_id" className="input font-mono" /></Field>
            <Field label="Mode">
              <select name="mode" defaultValue="cloud" className="input"><option value="cloud">cloud (real WhatsApp)</option><option value="dev">dev (simulated)</option></select>
            </Field>
            <div className="md:col-span-2"><Field label="Permanent access token" hint="Stored encrypted; never sent back to the browser. Leave blank to keep the existing token."><input name="access_token" type="password" autoComplete="off" className="input font-mono" /></Field></div>
            <div className="md:col-span-2"><button className="btn-primary">Connect</button></div>
          </form>
          <section className="card p-4 text-sm">
            <h2 className="mb-2 font-semibold">Meta webhook setup</h2>
            <ol className="list-decimal space-y-1 pl-5 text-ink-soft">
              <li>Expose the backend over HTTPS (e.g. a tunnel or your host).</li>
              <li>In Meta App → WhatsApp → Configuration, set Callback URL to <code className="rounded bg-canvas px-1">{origin}/webhooks/whatsapp</code></li>
              <li>Verify token = the <code className="rounded bg-canvas px-1">WHATSAPP_VERIFY_TOKEN</code> value in your backend <code>.env</code>.</li>
              <li>Subscribe to the <code>messages</code> field. Set <code className="rounded bg-canvas px-1">WHATSAPP_APP_SECRET</code> so signatures are verified.</li>
            </ol>
          </section>
        </div>
        {me?.features?.dev_tools && <Simulator />}
      </div>
    </div>
  );
}
