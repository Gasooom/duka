"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { FormEvent, Suspense, useState } from "react";
import { api, money, when } from "@/lib/api";
import { Badge, Empty, ErrorNote, NotLoaded, PageHeader, useApi } from "@/components/ui";

const STATUSES = ["pending", "accepted", "ready", "out_for_delivery", "delivered", "cancelled"];
const STATUS_LABEL: Record<string, string> = { pending: "needs review" };
// Mirrors backend ORDER_TRANSITIONS. Payment is separate and never set from here.
const NEXT: Record<string, string[]> = {
  pending: ["accepted", "cancelled"],
  accepted: ["ready", "out_for_delivery", "delivered", "cancelled"],
  ready: ["out_for_delivery", "delivered", "cancelled"],
  out_for_delivery: ["delivered"],
};
const ACTION_LABEL: Record<string, string> = {
  accepted: "Accept order", cancelled: "Reject / cancel", ready: "Ready", out_for_delivery: "Out for delivery",
  delivered: "Delivered",
};

function RecordPayment({ order, onDone }: { order: any; onDone: (fn: () => Promise<any>) => void }) {
  const [method, setMethod] = useState("momo");
  const reported = order.payments.find((p: any) => p.status === "pending" && p.provider === "manual");
  function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const f = new FormData(e.currentTarget);
    onDone(() => api(`/orders/${order.id}/payments`, {
      body: { method, reference: f.get("reference") || null, note: f.get("note") || null },
    }));
  }
  return (
    <form onSubmit={submit} className="mt-2 space-y-2 rounded-md border border-line p-3 text-sm">
      <div className="font-medium">Record a payment you received ({money(order.total, order.currency)})</div>
      {reported && <p className="text-xs text-amber-800">Customer reported reference <b>{reported.external_reference}</b>. Check your MoMo/bank before confirming.</p>}
      <select className="input" value={method} onChange={(e) => setMethod(e.target.value)}>
        <option value="momo">MTN MoMo / mobile money</option>
        <option value="cash">Cash</option>
        <option value="bank">Bank transfer</option>
        <option value="other">Other</option>
      </select>
      <input name="reference" className="input" defaultValue={reported?.external_reference || ""}
        placeholder={method === "cash" ? "Receipt number (optional)" : "Transaction ID (required)"} />
      <input name="note" className="input" placeholder={method === "cash" ? "Who received the cash (required)" : "Note (optional)"} />
      <button className="btn-primary py-1">Confirm payment received</button>
      <p className="text-xs text-ink-mute">Recorded with your name and the time. It can be voided later with a reason.</p>
    </form>
  );
}

function OrderDetail({ id, onChange }: { id: string; onChange: () => void }) {
  const { data: o, error, reload } = useApi<any>(`/orders/${id}`);
  const [err, setErr] = useState<string | null>(null);
  async function act(fn: () => Promise<any>) {
    setErr(null);
    try { await fn(); await reload(); onChange(); } catch (e: any) { setErr(e.message); }
  }
  function changeStatus(s: string) {
    if (s === "cancelled") {
      const reason = prompt("Reason for cancelling (sent to the customer):");
      if (reason === null) return;
      act(() => api(`/orders/${o.id}`, { method: "PATCH", body: { status: s, reason } }));
    } else {
      act(() => api(`/orders/${o.id}`, { method: "PATCH", body: { status: s } }));
    }
  }
  if (!o) return <div className="card p-4"><ErrorNote error={error} />Loading…</div>;
  const canPay = o.status !== "cancelled" && o.payment_status !== "paid";
  return (
    <div className="card p-4">
      <div className="flex items-start justify-between">
        <div>
          <div className="text-base font-semibold">{o.order_number}</div>
          <div className="text-xs text-ink-mute">{when(o.created_at)} · {o.customer.name || "Customer"} · +{o.customer.whatsapp_number}</div>
          {o.confirmed_at && <div className="text-xs text-ink-mute">Confirmed by the customer on WhatsApp {when(o.confirmed_at)}</div>}
        </div>
        <div className="flex flex-col items-end gap-1"><Badge value={o.status} /><Badge value={o.payment_status} /></div>
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
      <p className="mt-2 text-sm"><span className="text-ink-mute">{o.delivery_address ? "Deliver to:" : "Pickup"}</span> {o.delivery_address}</p>
      {o.cancel_reason && <p className="mt-1 text-sm text-red-700">Cancelled: {o.cancel_reason}</p>}

      {(NEXT[o.status] || []).length > 0 && (
        <>
          <h3 className="mb-1 mt-4 text-xs font-medium uppercase tracking-wide text-ink-mute">{o.status === "pending" ? "Review" : "Fulfilment"}</h3>
          <div className="flex flex-wrap gap-2">
            {NEXT[o.status].map((s) => (
              <button key={s} className={s === "cancelled" ? "btn-danger py-1" : s === "accepted" ? "btn-primary py-1" : "btn-ghost py-1"}
                onClick={() => changeStatus(s)}>{ACTION_LABEL[s] || s}</button>
            ))}
          </div>
          <p className="mt-1 text-xs text-ink-mute">The customer is told on WhatsApp.</p>
        </>
      )}

      <h3 className="mb-1 mt-4 text-xs font-medium uppercase tracking-wide text-ink-mute">Payments</h3>
      {o.payments.length === 0 && <p className="text-sm text-ink-mute">No payment yet.</p>}
      {o.payments.map((p: any) => (
        <div key={p.id} className="mb-2 rounded-md border border-line p-2 text-sm">
          <div className="flex justify-between">
            <span>{p.provider === "manual" ? `Manual · ${p.method || "reported by customer"}` : p.provider} · {money(p.amount, p.currency)}</span>
            <Badge value={p.status} />
          </div>
          {p.external_reference && <div className="font-mono text-xs">Ref {p.external_reference}</div>}
          {p.confirmation_source && <div className="text-xs text-ink-mute">Confirmed by {p.confirmation_source === "owner" ? "the shop (manual record)" : "the payment provider"} {when(p.confirmed_at)}</div>}
          {p.note && <div className="whitespace-pre-line text-xs text-ink-mute">{p.note}</div>}
          {p.failure_reason && <div className="text-xs text-red-700">{p.failure_reason}</div>}
          {p.status === "pending" && p.provider === "mock" && (
            <div className="mt-2 flex flex-wrap gap-2">
              <span className="w-full text-xs text-ink-mute">Dev: act as the payment provider and send a callback.</span>
              <button className="btn-ghost py-1" onClick={() => act(() => api(`/payments/${p.id}/simulate`, { body: { status: "successful" } }))}>Simulate success</button>
              <button className="btn-danger py-1" onClick={() => act(() => api(`/payments/${p.id}/simulate`, { body: { status: "failed" } }))}>Simulate failure</button>
            </div>
          )}
          {p.status === "pending" && p.provider === "momo" && (
            <button className="btn-ghost mt-2 py-1" onClick={() => act(() => api(`/payments/${p.id}/refresh`, { method: "POST" }))}>Check status with provider</button>
          )}
          {p.status === "successful" && p.provider === "manual" && (
            <button className="mt-1 text-xs text-red-700" onClick={() => {
              const reason = prompt("Why are you voiding this payment?");
              if (reason) act(() => api(`/payments/${p.id}/void`, { body: { reason } }));
            }}>Void (entered by mistake)</button>
          )}
        </div>
      ))}
      {canPay && <RecordPayment order={o} onDone={act} />}

      {o.audit.length > 0 && (
        <>
          <h3 className="mb-1 mt-4 text-xs font-medium uppercase tracking-wide text-ink-mute">History</h3>
          <ul className="space-y-1 text-xs text-ink-soft">
            {o.audit.map((e: any) => (
              <li key={e.id}>{when(e.created_at)} · {e.action.replace(/[._]/g, " ")}{e.data.to ? ` → ${e.data.to.replace(/_/g, " ")}` : ""} · {e.data.actor_email || e.actor_type}{e.data.reason ? ` · “${e.data.reason}”` : ""}</li>
            ))}
          </ul>
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
  const { data, error, reload, loading } = useApi<any[]>(`/orders${status ? `?status=${status}` : ""}`, 15000);
  const go = (q: Record<string, string>) => {
    const sp = new URLSearchParams({ ...(status ? { status } : {}), ...(selected ? { id: selected } : {}), ...q });
    for (const [k, v] of Array.from(sp.entries())) if (!v) sp.delete(k);
    router.replace(`/dashboard/orders?${sp}`);
  };
  return (
    <div>
      <PageHeader title="Orders" subtitle="New orders wait for your review. Payment is tracked separately." />
      <ErrorNote error={error} />
      <div className="mb-3 flex flex-wrap gap-1.5">
        {["", ...STATUSES].map((s) => (
          <button key={s} onClick={() => go({ status: s, id: "" })}
            className={`rounded-full border px-2.5 py-1 text-xs ${status === s ? "border-brand bg-brand-soft text-brand" : "border-line bg-white text-ink-soft"}`}>
            {s ? (STATUS_LABEL[s] || s.replace(/_/g, " ")) : "all"}
          </button>
        ))}
      </div>
      <div className="grid gap-5 lg:grid-cols-5">
        <div className="card overflow-x-auto lg:col-span-3">
          {!data ? <NotLoaded loading={loading} /> : data.length === 0 ? <Empty>No orders.</Empty> : (
            <table className="w-full">
              <thead><tr><th className="th">Order</th><th className="th">Status</th><th className="th">Payment</th><th className="th text-right">Total</th><th className="th text-right">Created</th></tr></thead>
              <tbody>
                {data?.map((o) => (
                  <tr key={o.id} onClick={() => go({ id: o.id })} className={`cursor-pointer hover:bg-canvas ${selected === o.id ? "bg-brand-soft/60" : ""}`}>
                    <td className="td font-medium">{o.order_number}<div className="text-xs text-ink-mute">{o.items.length} item(s)</div></td>
                    <td className="td"><Badge value={o.status} /></td>
                    <td className="td"><Badge value={o.payment_status} /></td>
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
