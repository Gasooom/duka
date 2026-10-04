"use client";

import Link from "next/link";
import { money, when } from "@/lib/api";
import { Badge, Empty, ErrorNote, PageHeader, useApi } from "@/components/ui";

function Stat({ label, value, href, tone }: { label: string; value: string | number; href?: string; tone?: "warn" }) {
  const body = (
    <div className={`card px-4 py-3 ${href ? "hover:border-ink-mute" : ""}`}>
      <div className="text-xs text-ink-mute">{label}</div>
      <div className={`mt-1 text-xl font-semibold tabular-nums ${tone === "warn" && value ? "text-amber-700" : ""}`}>{value}</div>
    </div>
  );
  return href ? <Link href={href}>{body}</Link> : body;
}

function SetupChecklist() {
  const { data } = useApi<any>("/dashboard/setup", 60000);
  if (!data || data.ready) return null;
  return (
    <section className="card mb-6 p-4">
      <h2 className="mb-2 text-sm font-semibold">Before real customers write in</h2>
      <ul className="space-y-1.5 text-sm">
        {data.checks.map((c: any) => (
          <li key={c.key} className="flex items-start gap-2">
            <span className={c.ok ? "text-emerald-700" : "text-amber-700"}>{c.ok ? "✓" : "!"}</span>
            {c.link && !c.ok ? <Link href={c.link} className="text-brand underline">{c.message}</Link> : <span className={c.ok ? "text-ink-soft" : ""}>{c.message}</span>}
          </li>
        ))}
      </ul>
    </section>
  );
}

function OwnerAlerts() {
  const { data } = useApi<any[]>("/dashboard/notifications?limit=8", 20000);
  return (
    <section className="card">
      <div className="border-b border-line px-4 py-3"><h2 className="text-sm font-semibold">Your alerts</h2></div>
      {!data || data.length === 0 ? <Empty>New orders, handoffs and reported payments appear here.</Empty> : (
        <ul>
          {data.map((n: any) => (
            <li key={n.id} className="border-b border-line px-4 py-2 text-sm last:border-0">
              <div className="flex items-center justify-between gap-2">
                <span className="truncate">{n.body.split("\n")[0]}</span>
                <Badge value={n.status} />
              </div>
              <div className="text-xs text-ink-mute">{when(n.created_at)}{n.error ? ` · ${n.error}` : ""}{n.status === "skipped" ? " · add your WhatsApp number in Settings to receive these" : ""}</div>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

export default function Overview() {
  const { data: s, error } = useApi<any>("/dashboard/stats", 20000);
  return (
    <div>
      <PageHeader title="Overview" subtitle="Live numbers from your store. Revenue counts confirmed payments only." />
      <ErrorNote error={error} />
      <SetupChecklist />
      {s && (
        <>
          <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
            <Stat label="Revenue (paid)" value={money(s.revenue, s.currency)} />
            <Stat label="Orders" value={s.orders_total} href="/dashboard/orders" />
            <Stat label="Orders to review" value={s.orders_awaiting_review} href="/dashboard/orders?status=pending" tone="warn" />
            <Stat label="Needs human attention" value={s.needs_attention} href="/dashboard/conversations?attention=1" tone="warn" />
            <Stat label="Customers" value={s.customers} href="/dashboard/customers" />
            <Stat label="Messages" value={s.messages} href="/dashboard/conversations" />
            <Stat label="Products" value={s.products} href="/dashboard/products" />
            <Stat label="Low stock" value={s.low_stock.length} href="/dashboard/products" tone="warn" />
          </div>

          <div className="mt-6 grid gap-6 lg:grid-cols-3">
            <section className="card lg:col-span-2">
              <div className="flex items-center justify-between border-b border-line px-4 py-3">
                <h2 className="text-sm font-semibold">Recent orders</h2>
                <Link href="/dashboard/orders" className="text-xs text-brand">All orders</Link>
              </div>
              {s.recent_orders.length === 0 ? <Empty>No orders yet.</Empty> : (
                <table className="w-full">
                  <tbody>
                    {s.recent_orders.map((o: any) => (
                      <tr key={o.id}>
                        <td className="td font-medium"><Link href={`/dashboard/orders?id=${o.id}`}>{o.order_number}</Link></td>
                        <td className="td"><Badge value={o.status} /></td>
                        <td className="td text-right tabular-nums">{money(o.total, o.currency)}</td>
                        <td className="td text-right text-ink-mute">{when(o.created_at)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </section>
            <section className="card">
              <div className="border-b border-line px-4 py-3"><h2 className="text-sm font-semibold">Low stock</h2></div>
              {s.low_stock.length === 0 ? <Empty>All products above threshold.</Empty> : (
                <ul>
                  {s.low_stock.map((p: any) => (
                    <li key={p.id} className="flex justify-between border-b border-line px-4 py-2 last:border-0">
                      <span className="truncate pr-2">{p.name}</span>
                      <span className={`tabular-nums ${p.stock_quantity === 0 ? "text-red-700" : "text-amber-700"}`}>{p.stock_quantity}</span>
                    </li>
                  ))}
                </ul>
              )}
            </section>
          </div>

          <div className="mt-6"><OwnerAlerts /></div>

          <section className="card mt-6 p-4">
            <h2 className="mb-2 text-sm font-semibold">Orders by status</h2>
            <div className="flex flex-wrap gap-4 text-sm">
              {Object.entries(s.orders_by_status).map(([k, v]: any) => (
                <Link key={k} href={`/dashboard/orders?status=${k}`} className="flex items-center gap-2">
                  <Badge value={k} /> <span className="tabular-nums">{v}</span>
                </Link>
              ))}
            </div>
          </section>
        </>
      )}
    </div>
  );
}
