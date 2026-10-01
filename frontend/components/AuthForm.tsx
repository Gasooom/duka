"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { FormEvent, useState } from "react";
import { api, setToken } from "@/lib/api";
import { ErrorNote, Field } from "@/components/ui";

export default function AuthForm({ mode }: { mode: "login" | "register" }) {
  const router = useRouter();
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const f = Object.fromEntries(new FormData(e.currentTarget)) as Record<string, string>;
    setBusy(true);
    setError(null);
    try {
      const res = await api<{ access_token: string }>(`/auth/${mode}`, { body: f });
      setToken(res.access_token);
      router.push(mode === "register" ? "/dashboard/business" : "/dashboard");
    } catch (err: any) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center px-4">
      <div className="w-full max-w-sm">
        <div className="mb-6">
          <div className="text-lg font-semibold">Duka</div>
          <div className="text-sm text-ink-mute">WhatsApp commerce assistant for your business</div>
        </div>
        <form onSubmit={submit} className="card space-y-3 p-5">
          <h1 className="text-base font-semibold">{mode === "login" ? "Sign in" : "Create your business"}</h1>
          <ErrorNote error={error} />
          {mode === "register" && (
            <>
              <Field label="Business name"><input name="business_name" required className="input" /></Field>
              <div className="grid grid-cols-2 gap-3">
                <Field label="Type">
                  <select name="business_type" className="input" defaultValue="retail">
                    {["retail", "clothing", "electronics", "restaurant", "grocery", "salon", "services"].map((t) => (
                      <option key={t}>{t}</option>
                    ))}
                  </select>
                </Field>
                <Field label="Currency"><input name="currency" defaultValue="RWF" maxLength={3} className="input" /></Field>
              </div>
              <Field label="Your name"><input name="full_name" className="input" /></Field>
            </>
          )}
          <Field label="Email"><input name="email" type="email" required className="input" /></Field>
          <Field label="Password"><input name="password" type="password" required minLength={8} className="input" /></Field>
          <button disabled={busy} className="btn-primary w-full">{busy ? "Please wait…" : mode === "login" ? "Sign in" : "Create account"}</button>
          <p className="text-center text-xs text-ink-mute">
            {mode === "login" ? (
              <>No account? <Link className="text-brand underline" href="/register">Register your business</Link></>
            ) : (
              <>Already registered? <Link className="text-brand underline" href="/login">Sign in</Link></>
            )}
          </p>
        </form>
      </div>
    </div>
  );
}
