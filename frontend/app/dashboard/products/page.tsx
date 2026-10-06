"use client";

import { FormEvent, useState } from "react";
import { api, money } from "@/lib/api";
import { Badge, Empty, ErrorNote, Field, NotLoaded, OkNote, PageHeader, useApi } from "@/components/ui";

type Product = {
  id: string; name: string; description: string | null; price: number; currency: string; sku: string;
  category: string | null; stock_quantity: number; image_url: string | null; active: boolean;
};

function ProductForm({ product, onDone }: { product: Product | null; onDone: () => void }) {
  const [error, setError] = useState<string | null>(null);
  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const f = Object.fromEntries(new FormData(e.currentTarget)) as Record<string, any>;
    const body: Record<string, any> = { ...f, price: Number(f.price), stock_quantity: Number(f.stock_quantity || 0), active: f.active === "on" };
    for (const k of ["description", "category", "image_url", "sku"]) if (!body[k]) body[k] = product ? null : undefined;
    try {
      if (product) await api(`/products/${product.id}`, { method: "PATCH", body });
      else await api("/products", { body });
      onDone();
    } catch (err: any) {
      setError(err.message);
    }
  }
  return (
    <form onSubmit={submit} className="card mb-5 grid gap-3 p-4 md:grid-cols-3">
      <div className="md:col-span-3"><ErrorNote error={error} /></div>
      <Field label="Name"><input name="name" required defaultValue={product?.name} className="input" /></Field>
      <Field label="Price"><input name="price" type="number" min={0} step="any" required defaultValue={product?.price} className="input" /></Field>
      <Field label="SKU" hint={product ? undefined : "Auto-generated if empty"}><input name="sku" defaultValue={product?.sku} className="input" /></Field>
      <Field label="Category"><input name="category" defaultValue={product?.category || ""} className="input" /></Field>
      <Field label="Stock quantity"><input name="stock_quantity" type="number" min={0} defaultValue={product?.stock_quantity ?? 0} className="input" /></Field>
      <Field label="Image URL"><input name="image_url" defaultValue={product?.image_url || ""} className="input" /></Field>
      <div className="md:col-span-3">
        <Field label="Description" hint="Used by search and the AI — mention colour, size, material.">
          <textarea name="description" rows={2} defaultValue={product?.description || ""} className="input" />
        </Field>
      </div>
      <label className="flex items-center gap-2 text-sm"><input type="checkbox" name="active" defaultChecked={product?.active ?? true} /> Active (visible to customers)</label>
      <div className="flex justify-end gap-2 md:col-span-2">
        <button type="button" className="btn-ghost" onClick={onDone}>Cancel</button>
        <button className="btn-primary">{product ? "Save changes" : "Add product"}</button>
      </div>
    </form>
  );
}

function CsvImport({ onDone }: { onDone: () => void }) {
  const [result, setResult] = useState<any>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  async function upload(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const fd = new FormData(e.currentTarget);
    const skip = fd.get("skip_invalid") === "on";
    fd.delete("skip_invalid");
    setBusy(true); setError(null); setResult(null);
    try {
      const r = await api(`/products/import?skip_invalid=${skip}`, { form: fd });
      setResult(r);
      if (r.imported) onDone();
    } catch (err: any) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="card mb-5 p-4">
      <form onSubmit={upload} className="flex flex-wrap items-end gap-3">
        <Field label="CSV file" hint="Columns: name,description,price,category,sku,stock_quantity (image_url, active optional). Existing SKUs are updated.">
          <input type="file" name="file" accept=".csv,text/csv" required className="text-sm" />
        </Field>
        <label className="flex items-center gap-2 text-sm"><input type="checkbox" name="skip_invalid" /> Import valid rows, skip invalid</label>
        <button disabled={busy} className="btn-primary">{busy ? "Importing…" : "Import"}</button>
      </form>
      <div className="mt-3"><ErrorNote error={error} /></div>
      {result && (
        <div className="text-sm">
          {result.imported
            ? <OkNote text={`Imported: ${result.created} created, ${result.updated} updated (${result.total_rows} rows).`} />
            : <ErrorNote error={`Nothing imported — fix ${result.errors.length} error(s) below or tick "skip invalid".`} />}
          {result.errors.length > 0 && (
            <table className="w-full">
              <thead><tr><th className="th">Row</th><th className="th">Column</th><th className="th">Problem</th></tr></thead>
              <tbody>{result.errors.map((e: any, i: number) => (
                <tr key={i}><td className="td">{e.row}</td><td className="td">{e.field || "—"}</td><td className="td">{e.message}</td></tr>
              ))}</tbody>
            </table>
          )}
        </div>
      )}
    </div>
  );
}

export default function Products() {
  const [q, setQ] = useState("");
  const { data, error, reload, loading } = useApi<Product[]>(`/products${q ? `?q=${encodeURIComponent(q)}` : ""}`);
  const [editing, setEditing] = useState<Product | "new" | null>(null);
  const [showImport, setShowImport] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  async function act(fn: () => Promise<any>) {
    setActionError(null);
    try { await fn(); reload(); } catch (e: any) { setActionError(e.message); }
  }

  return (
    <div>
      <PageHeader title="Products" subtitle={data ? `${data.length} products` : undefined}
        actions={<>
          <button className="btn-ghost" onClick={() => setShowImport(!showImport)}>Import CSV</button>
          <button className="btn-primary" onClick={() => setEditing("new")}>Add product</button>
        </>} />
      {showImport && <CsvImport onDone={reload} />}
      {editing && <ProductForm key={editing === "new" ? "new" : editing.id} product={editing === "new" ? null : editing}
        onDone={() => { setEditing(null); reload(); }} />}
      <ErrorNote error={error || actionError} />
      <input placeholder="Filter by name or SKU…" value={q} onChange={(e) => setQ(e.target.value)} className="input mb-3 max-w-xs" />
      <div className="card overflow-x-auto">
        {!data ? <NotLoaded loading={loading} /> : data.length === 0 ? <Empty>No products yet. Add one or import a CSV.</Empty> : (
          <table className="w-full min-w-[720px]">
            <thead><tr>
              <th className="th">Product</th><th className="th">SKU</th><th className="th">Category</th>
              <th className="th text-right">Price</th><th className="th text-right">Stock</th><th className="th">Status</th><th className="th"></th>
            </tr></thead>
            <tbody>
              {data?.map((p) => (
                <tr key={p.id} className={p.active ? "" : "text-ink-mute"}>
                  <td className="td"><div className="font-medium">{p.name}</div><div className="max-w-sm truncate text-xs text-ink-mute">{p.description}</div></td>
                  <td className="td font-mono text-xs">{p.sku}</td>
                  <td className="td">{p.category || "—"}</td>
                  <td className="td text-right tabular-nums">{money(p.price, p.currency)}</td>
                  <td className="td text-right">
                    <div className="flex items-center justify-end gap-1">
                      <button className="btn-ghost px-1.5 py-0.5" title="Remove 1" onClick={() => act(() => api(`/products/${p.id}/stock`, { body: { change: -1 } }))}>−</button>
                      <span className={`w-8 text-center tabular-nums ${p.stock_quantity <= 5 ? "text-amber-700" : ""}`}>{p.stock_quantity}</span>
                      <button className="btn-ghost px-1.5 py-0.5" title="Add 1" onClick={() => act(() => api(`/products/${p.id}/stock`, { body: { change: 1 } }))}>+</button>
                    </div>
                  </td>
                  <td className="td"><Badge value={p.active ? "active" : "inactive"} /></td>
                  <td className="td whitespace-nowrap text-right">
                    <button className="text-xs text-brand" onClick={() => setEditing(p)}>Edit</button>
                    <button className="ml-3 text-xs text-ink-mute" onClick={() => act(() => api(`/products/${p.id}`, { method: "PATCH", body: { active: !p.active } }))}>
                      {p.active ? "Deactivate" : "Activate"}
                    </button>
                    <button className="ml-3 text-xs text-red-700" onClick={() => confirm(`Delete ${p.name}?`) && act(() => api(`/products/${p.id}`, { method: "DELETE" }))}>Delete</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
