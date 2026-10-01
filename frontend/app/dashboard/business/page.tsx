"use client";

import { FormEvent, useState } from "react";
import { api } from "@/lib/api";
import { ErrorNote, Field, OkNote, PageHeader, useApi } from "@/components/ui";

function hoursToText(h: Record<string, string> | undefined) {
  return Object.entries(h || {}).map(([k, v]) => `${k}: ${v}`).join("\n");
}
function textToHours(t: string) {
  return Object.fromEntries(t.split("\n").map((l) => l.split(/:(.+)/).map((s) => s.trim())).filter((p) => p[0] && p[1]));
}

export default function BusinessPage() {
  const { data: b, error, reload } = useApi<any>("/business");
  const { data: cfg, reload: reloadCfg } = useApi<any>("/business/agent-config");
  const [msg, setMsg] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  async function saveBusiness(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const f = Object.fromEntries(new FormData(e.currentTarget)) as Record<string, any>;
    const body: any = { ...f, business_hours: textToHours(f.business_hours || "") };
    for (const k of ["delivery_enabled", "payment_enabled", "human_handoff_enabled"]) body[k] = f[k] === "on";
    try { await api("/business", { method: "PATCH", body }); setMsg("Business profile saved."); setErr(null); reload(); }
    catch (e: any) { setErr(e.message); setMsg(null); }
  }
  async function saveAgent(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const f = Object.fromEntries(new FormData(e.currentTarget)) as Record<string, any>;
    const body = { ...f, temperature: Number(f.temperature), max_history_messages: Number(f.max_history_messages), model: f.model || null };
    try { await api("/business/agent-config", { method: "PATCH", body }); setMsg("AI assistant configuration saved."); setErr(null); reloadCfg(); }
    catch (e: any) { setErr(e.message); setMsg(null); }
  }

  return (
    <div className="max-w-3xl">
      <PageHeader title="Business & AI assistant" subtitle="Everything the assistant knows about your business comes from here, your products and your knowledge base." />
      <ErrorNote error={error || err} />
      <OkNote text={msg} />
      {b && (
        <form onSubmit={saveBusiness} className="card mb-6 grid gap-3 p-4 md:grid-cols-2">
          <h2 className="text-sm font-semibold md:col-span-2">Business profile</h2>
          <Field label="Name"><input name="name" defaultValue={b.name} className="input" required /></Field>
          <Field label="Type"><input name="business_type" defaultValue={b.business_type} className="input" /></Field>
          <div className="md:col-span-2"><Field label="Description"><textarea name="description" rows={2} defaultValue={b.description || ""} className="input" /></Field></div>
          <Field label="Phone"><input name="phone" defaultValue={b.phone || ""} className="input" /></Field>
          <Field label="Address"><input name="address" defaultValue={b.address || ""} className="input" /></Field>
          <Field label="Logo URL"><input name="logo_url" defaultValue={b.logo_url || ""} className="input" /></Field>
          <div className="grid grid-cols-3 gap-2">
            <Field label="Currency"><input name="currency" defaultValue={b.currency} maxLength={3} className="input" /></Field>
            <Field label="Language"><input name="language" defaultValue={b.language} className="input" /></Field>
            <Field label="Order prefix"><input name="order_prefix" defaultValue={b.order_prefix} maxLength={6} className="input" /></Field>
          </div>
          <Field label="Timezone"><input name="timezone" defaultValue={b.timezone} className="input" /></Field>
          <Field label="Business hours" hint="One per line, e.g. Mon-Fri: 08:00-18:00">
            <textarea name="business_hours" rows={3} defaultValue={hoursToText(b.business_hours)} className="input font-mono text-xs" />
          </Field>
          <div className="flex flex-wrap gap-4 text-sm md:col-span-2">
            <label className="flex items-center gap-2"><input type="checkbox" name="delivery_enabled" defaultChecked={b.delivery_enabled} /> Delivery</label>
            <label className="flex items-center gap-2"><input type="checkbox" name="payment_enabled" defaultChecked={b.payment_enabled} /> Online payment</label>
            <label className="flex items-center gap-2"><input type="checkbox" name="human_handoff_enabled" defaultChecked={b.human_handoff_enabled} /> Human handoff</label>
          </div>
          <div className="md:col-span-2"><button className="btn-primary">Save profile</button></div>
        </form>
      )}
      {cfg && (
        <form onSubmit={saveAgent} className="card grid gap-3 p-4 md:grid-cols-2">
          <h2 className="text-sm font-semibold md:col-span-2">AI assistant</h2>
          <Field label="Tone"><input name="tone" defaultValue={cfg.tone} className="input" /></Field>
          <Field label="Reply language"><input name="language" defaultValue={cfg.language} className="input" /></Field>
          <div className="md:col-span-2"><Field label="Greeting" hint="Sent for plain greetings without calling the LLM."><textarea name="greeting" rows={2} defaultValue={cfg.greeting} className="input" /></Field></div>
          <div className="md:col-span-2"><Field label="Fallback message" hint="Sent when the AI fails."><textarea name="fallback_message" rows={2} defaultValue={cfg.fallback_message} className="input" /></Field></div>
          <div className="md:col-span-2"><Field label="Business rules" hint="Short rules the assistant must follow, e.g. exchange policy or upsell guidance."><textarea name="business_rules" rows={3} defaultValue={cfg.business_rules || ""} className="input" /></Field></div>
          <div className="md:col-span-2"><Field label="Extra instructions (system prompt)"><textarea name="system_prompt" rows={3} defaultValue={cfg.system_prompt || ""} className="input" /></Field></div>
          <Field label="Model override" hint="Blank = platform default (LLM_MODEL)"><input name="model" defaultValue={cfg.model || ""} className="input" /></Field>
          <div className="grid grid-cols-2 gap-2">
            <Field label="Temperature"><input name="temperature" type="number" step="0.05" min={0} max={1.5} defaultValue={cfg.temperature} className="input" /></Field>
            <Field label="History messages"><input name="max_history_messages" type="number" min={2} max={30} defaultValue={cfg.max_history_messages} className="input" /></Field>
          </div>
          <div className="md:col-span-2"><button className="btn-primary">Save assistant</button></div>
        </form>
      )}
    </div>
  );
}
