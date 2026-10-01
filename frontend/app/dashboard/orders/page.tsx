"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useState } from "react";
import { api, money, when } from "@/lib/api";
import { Badge, Empty, ErrorNote, PageHeader, useApi } from "@/components/ui";

const STATUSES = ["pending", "awaiting_payment", "paid", "processing", "ready", "out_for_delivery", "delivered", "cancelled"];
// Mirrors backend ORDER_TRANSITIONS minus provider-only targets ('paid', 'awaiting_payment').
const NEXT: Record<string, string[]> = {
  pending: ["processing", "cancelled"],
  awaiting_payment: ["cancelled"],
  paid: ["processing", "ready", "out_for_delivery"],
  processing: ["ready", "out_for_delivery", "cancelled"],
  ready: ["out_for_delivery", "delivered"],
  out_for_delivery: ["delivered"],
};

function OrderDetail({ id, onChange }: { id: string; onChange: () => void }) {
  const { data: o, error, reload } = useApi<any>(`/orders/${id}`);
  const [err, setErr] = useState<string | null>(null);
  async function act(fn: () => Promise<any>) {
    setErr(null);
    try { await fn(); await reload(); onChange(); } catch (e: any) { setErr(e.message); }
  }
  if (!o) return <div className="card p-4"><ErrorNote error={error} />Loading…</div>;
  return (
    <div className="card p-4">
      <div className="flex items-start justify-between">
        <div>
          <div className="text-base font-semibold">{o.order_number}</div>
          <div className="text-xs text-ink-mute">{when(o.created_at)} · {o.customer.name || "Customer"} · {o.customer.whatsapp_number}</div>
        </div>
        <Badge value={o.status} />
      </div>
      <ErrorNote error={err} />
      <table className="mt-3 w-full">
        <tbody>
          {o.items.map((i: any, n: number) => (
            <tr key={n}>
              <td className="td">{i.product_name} <span className="text-ink-mute">× {i.quantity}</span><div className="font-mono text-xs text-ink-mute">{i.sku}</div></td>
              <td className="td text-right tabular-nums">{money(i.subtotal, o.currency)}</td>
            </tr>
          ))}
          <tr><td className="td text-ink-mute">Subtotal</td><td className="td text-right tabular-nums">{money(o.subtotal, o.currency)}</td></tr>
          <tr><td className="td text-ink-mute">Delivery {o.delivery_zone_name ? `(${o.delivery_zone_name})` : ""}</td><td className="td text-right tabular-nums">{money(o.delivery_fee, o.currency)}</td></tr>
          {o.discount > 0 && <tr><td className="td text-ink-mute">Discount</td><td className="td text-right">−{money(o.discount, o.currency)}</td></tr>}
          <tr><td className="td font-semibold">Total</td><td className="td text-right font-semibold tabular-nums">{money(o.total, o.currency)}</td></tr>
        </tbody>
      </table>
      {o.delivery_address && <p className="mt-2 text-sm"><span className="text-ink-mute">Deliver to:</span> {o.delivery_address}</p>}

      <h3 className="mb-1 mt-4 text-xs font-medium uppercase tracking-wide text-ink-mute">Payments</h3>
      {o.payments.length === 0 ? <p className="text-sm text-ink-mute">No payment initiated.</p> : o.payments.map((p: any) => (
        <div key={p.id} className="mb-2 rounded-md border border-line p-2 text-sm">
          <div className="flex justify-between"><span>{p.provider} · {money(p.amount, p.currency)} · {p.payer_phone}</span><Badge value={p.status} /></div>
          <div className="font-mono text-xs text-ink-mute">{p.provider_reference}</div>
          {p.failure_reason && <div className="text-xs text-red-700">{p.failure_reason}</div>}
          {p.status === "pending" && (
            <div className="mt-2 flex flex-wrap gap-2">
              {p.provider === "mock" ? (
                <>
                  <span className="w-full text-xs text-ink-mute">Dev: act as the payment provider and send a callback.</span>
                  <button className="btn-ghost py-1" onClick={() => act(() => api(`/payments/${p.id}/simulate`, { body: { status: "successful" } }))}>Simulate success</button>
                  <button className="btn-danger py-1" onClick={() => act(() => api(`/payments/${p.id}/simulate`, { body: { status: "failed" } }))}>Simulate failure</button>
                </>
              ) : (
                <button className="btn-ghost py-1" onClick={() => act(() => api(`/payments/${p.id}/refresh`, { method: "POST" }))}>Check status with provider</button>
              )}
            </div>
          )}
        </div>
      ))}

      {(NEXT[o.status] || []).length > 0 && (
        <>
          <h3 className="mb-1 mt-4 text-xs font-medium uppercase tracking-wide text-ink-mute">Update status</h3>
          <div className="flex flex-wrap gap-2">
            {NEXT[o.status].map((s) => (
              <button key={s} className={s === "cancelled" ? "btn-danger py-1" : "btn-ghost py-1"}
                onClick={() => (s !== "cancelled" || confirm("Cancel this order and restock items?")) && act(() => api(`/orders/${o.id}`, { method: "PATCH", body: { status: s } }))}>
                {s.replace(/_/g, " ")}
              </button>
            ))}
          </div>
          <p className="mt-2 text-xs text-ink-mute">“Paid” is only set when the payment provider confirms.</p>
        </>
      )}
    </div>
  );
}

function OrdersInner() {
  const params = useSearchParams();
  const router = useRouter();
  const status = params.get("status") || "";
  const selected = params.get("id");
  const { data, error, reload } = useApi<any[]>(`/orders${status ? `?status=${status}` : ""}`);
  const go = (q: Record<string, string>) => {
    const sp = new URLSearchParams({ ...(status ? { status } : {}), ...(selected ? { id: selected } : {}), ...q });
    for (const [k, v] of Array.from(sp.entries())) if (!v) sp.delete(k);
    router.replace(`/dashboard/orders?${sp}`);
  };
  return (
    <div>
      <PageHeader title="Orders" />
      <ErrorNote error={error} />
      <div className="mb-3 flex flex-wrap gap-1.5">
        {["", ...STATUSES].map((s) => (
          <button key={s} onClick={() => go({ status: s, id: "" })}
            className={`rounded-full border px-2.5 py-1 text-xs ${status === s ? "border-brand bg-brand-soft text-brand" : "border-line bg-white text-ink-soft"}`}>
            {s ? s.replace(/_/g, " ") : "all"}
          </button>
        ))}
      </div>
      <div className="grid gap-5 lg:grid-cols-5">
        <div className="card overflow-x-auto lg:col-span-3">
          {data && data.length === 0 ? <Empty>No orders.</Empty> : (
            <table className="w-full">
              <thead><tr><th className="th">Order</th><th className="th">Status</th><th className="th text-right">Total</th><th className="th text-right">Created</th></tr></thead>
              <tbody>
                {data?.map((o) => (
                  <tr key={o.id} onClick={() => go({ id: o.id })} className={`cursor-pointer hover:bg-canvas ${selected === o.id ? "bg-brand-soft/60" : ""}`}>
                    <td className="td font-medium">{o.order_number}<div className="text-xs text-ink-mute">{o.items.length} item(s)</div></td>
                    <td className="td"><Badge value={o.status} /></td>
                    <td className="td text-right tabular-nums">{money(o.total, o.currency)}</td>
                    <td className="td text-right text-ink-mute">{when(o.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
        <div className="lg:col-span-2">
          {selected ? <OrderDetail key={selected} id={selected} onChange={reload} /> : <div className="card"><Empty>Select an order to see details.</Empty></div>}
        </div>
      </div>
    </div>
  );
}

export default function Orders() {
  return <Suspense><OrdersInner /></Suspense>;
}
