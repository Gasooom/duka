"use client";

import { FormEvent, useState } from "react";
import { api, money } from "@/lib/api";
import { Empty, ErrorNote, Field, OkNote, PageHeader, useApi } from "@/components/ui";

export default function SettingsPage() {
  const { data: s, reload } = useApi<any>("/business/settings");
  const { data: zones, reload: reloadZones } = useApi<any[]>("/delivery-zones");
  const { data: biz } = useApi<any>("/business");
  const [err, setErr] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [editing, setEditing] = useState<any | null>(null);

  async function act(fn: () => Promise<any>, ok?: string) {
    setErr(null); setMsg(null);
    try { await fn(); if (ok) setMsg(ok); reload(); reloadZones(); } catch (e: any) { setErr(e.message); }
  }
  function saveSettings(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const f = Object.fromEntries(new FormData(e.currentTarget)) as Record<string, any>;
    act(() => api("/business/settings", { method: "PATCH", body: { payment_provider: f.payment_provider,
      low_stock_threshold: Number(f.low_stock_threshold), max_order_quantity: Number(f.max_order_quantity) } }), "Settings saved.");
  }
  function saveZone(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const f = Object.fromEntries(new FormData(e.currentTarget)) as Record<string, any>;
    const body = { name: f.name, fee: Number(f.fee), estimated_time: f.estimated_time || null, is_default: f.is_default === "on",
      active: true, areas: String(f.areas || "").split(",").map((a) => a.trim()).filter(Boolean) };
    act(async () => {
      if (editing?.id) await api(`/delivery-zones/${editing.id}`, { method: "PATCH", body });
      else await api("/delivery-zones", { body });
      setEditing(null);
    }, "Delivery zone saved.");
  }

  return (
    <div className="max-w-3xl">
      <PageHeader title="Settings" />
      <ErrorNote error={err} />
      <OkNote text={msg} />
      {s && (
        <form onSubmit={saveSettings} className="card mb-6 grid gap-3 p-4 md:grid-cols-3">
          <h2 className="text-sm font-semibold md:col-span-3">Payments & inventory</h2>
          <Field label="Payment provider" hint="Mock = dev callbacks; MTN MoMo needs MOMO_* env credentials.">
            <select name="payment_provider" defaultValue={s.payment_provider} className="input">
              <option value="mock">Mock (development)</option>
              <option value="momo">MTN Mobile Money</option>
            </select>
          </Field>
          <Field label="Low-stock threshold"><input name="low_stock_threshold" type="number" min={0} defaultValue={s.low_stock_threshold} className="input" /></Field>
          <Field label="Max units per product"><input name="max_order_quantity" type="number" min={1} defaultValue={s.max_order_quantity} className="input" /></Field>
          <div className="md:col-span-3"><button className="btn-primary">Save</button></div>
        </form>
      )}

      <section className="card">
        <div className="flex items-center justify-between border-b border-line px-4 py-3">
          <h2 className="text-sm font-semibold">Delivery zones</h2>
          <button className="btn-ghost py-1" onClick={() => setEditing({})}>Add zone</button>
        </div>
        {editing && (
          <form key={editing.id || "new"} onSubmit={saveZone} className="grid gap-3 border-b border-line p-4 md:grid-cols-2">
            <Field label="Zone name"><input name="name" defaultValue={editing.name} required className="input" /></Field>
            <Field label={`Fee (${biz?.currency || ""})`}><input name="fee" type="number" min={0} step="any" defaultValue={editing.fee} required className="input" /></Field>
            <Field label="Areas covered" hint="Comma-separated, matched against the customer's location."><input name="areas" defaultValue={(editing.areas || []).join(", ")} className="input" /></Field>
            <Field label="Estimated time"><input name="estimated_time" defaultValue={editing.estimated_time || ""} className="input" /></Field>
            <label className="flex items-center gap-2 text-sm"><input type="checkbox" name="is_default" defaultChecked={editing.is_default} /> Default zone (used when location is unknown)</label>
            <div className="flex justify-end gap-2"><button type="button" className="btn-ghost" onClick={() => setEditing(null)}>Cancel</button><button className="btn-primary">Save zone</button></div>
          </form>
        )}
        {zones && zones.length === 0 ? <Empty>No delivery zones. Orders need one when delivery is enabled.</Empty> : (
          <table className="w-full">
            <tbody>
              {zones?.map((z) => (
                <tr key={z.id}>
                  <td className="td"><div className="font-medium">{z.name} {z.is_default && <span className="text-xs text-brand">default</span>}</div><div className="text-xs text-ink-mute">{z.areas.join(", ")}</div></td>
                  <td className="td tabular-nums">{money(z.fee, biz?.currency)}</td>
                  <td className="td text-ink-mute">{z.estimated_time}</td>
                  <td className="td whitespace-nowrap text-right">
                    <button className="text-xs text-brand" onClick={() => setEditing(z)}>Edit</button>
                    <button className="ml-3 text-xs text-red-700" onClick={() => confirm(`Delete ${z.name}?`) && act(() => api(`/delivery-zones/${z.id}`, { method: "DELETE" }))}>Delete</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </div>
  );
}
