"use client";

import { FormEvent, useState } from "react";
import { api, setToken } from "@/lib/api";
import { ErrorNote, Field, OkNote, PageHeader, useApi } from "@/components/ui";

export default function AccountPage() {
  const { data: me } = useApi<any>("/auth/me");
  const [err, setErr] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);

  async function change(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const form = e.currentTarget;
    const f = Object.fromEntries(new FormData(form)) as Record<string, string>;
    setErr(null); setMsg(null);
    if (f.new_password !== f.confirm) { setErr("The new passwords do not match."); return; }
    try {
      const r = await api<any>("/auth/change-password", { body: { current_password: f.current_password, new_password: f.new_password } });
      setToken(r.access_token);
      form.reset();
      setMsg("Password changed. Other devices have been signed out.");
    } catch (er: any) { setErr(er.message); }
  }

  return (
    <div className="max-w-md">
      <PageHeader title="Account" subtitle={me ? `${me.user.email} · ${me.user.role}` : ""} />
      <ErrorNote error={err} />
      <OkNote text={msg} />
      <form onSubmit={change} className="card space-y-3 p-4">
        <h2 className="text-sm font-semibold">Change password</h2>
        <Field label="Current password"><input name="current_password" type="password" required className="input" autoComplete="current-password" /></Field>
        <Field label="New password" hint="At least 8 characters."><input name="new_password" type="password" minLength={8} required className="input" autoComplete="new-password" /></Field>
        <Field label="Repeat new password"><input name="confirm" type="password" minLength={8} required className="input" autoComplete="new-password" /></Field>
        <button className="btn-primary">Change password</button>
      </form>
      <p className="mt-3 text-xs text-ink-mute">Forgot your password? Ask the Duka team to reset it.</p>
    </div>
  );
}
