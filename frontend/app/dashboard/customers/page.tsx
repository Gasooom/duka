"use client";

import { useState } from "react";
import { money, when } from "@/lib/api";
import { Empty, ErrorNote, NotLoaded, PageHeader, useApi } from "@/components/ui";

export default function Customers() {
  const [q, setQ] = useState("");
  const { data, error, loading } = useApi<any[]>(`/customers${q ? `?q=${encodeURIComponent(q)}` : ""}`);
  const { data: biz } = useApi<any>("/business");
  return (
    <div>
      <PageHeader title="Customers" subtitle="Created automatically from WhatsApp numbers." />
      <ErrorNote error={error} />
      <input placeholder="Search name or number…" value={q} onChange={(e) => setQ(e.target.value)} className="input mb-3 max-w-xs" />
      <div className="card overflow-x-auto">
        {!data ? <NotLoaded loading={loading} /> : data.length === 0 ? <Empty>No customers yet.</Empty> : (
          <table className="w-full">
            <thead><tr><th className="th">Name</th><th className="th">WhatsApp</th><th className="th text-right">Orders</th><th className="th text-right">Paid total</th><th className="th text-right">First seen</th></tr></thead>
            <tbody>
              {data?.map((c) => (
                <tr key={c.id}>
                  <td className="td font-medium">{c.name || "—"}</td>
                  <td className="td font-mono text-xs">+{c.whatsapp_number}</td>
                  <td className="td text-right tabular-nums">{c.order_count}</td>
                  <td className="td text-right tabular-nums">{money(c.total_spent, biz?.currency)}</td>
                  <td className="td text-right text-ink-mute">{when(c.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
