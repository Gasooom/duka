"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { ReactNode, useEffect, useState } from "react";
import { api, getToken, setToken } from "@/lib/api";

const NAV = [
  { href: "/dashboard", label: "Overview" },
  { href: "/dashboard/orders", label: "Orders" },
  { href: "/dashboard/conversations", label: "Conversations" },
  { href: "/dashboard/products", label: "Products" },
  { href: "/dashboard/customers", label: "Customers" },
  { href: "/dashboard/knowledge", label: "Knowledge" },
  { href: "/dashboard/whatsapp", label: "WhatsApp" },
  { href: "/dashboard/business", label: "Business & AI" },
  { href: "/dashboard/settings", label: "Settings" },
];

export default function DashboardLayout({ children }: { children: ReactNode }) {
  const path = usePathname();
  const router = useRouter();
  const [me, setMe] = useState<any>(null);
  const [open, setOpen] = useState(false);

  useEffect(() => {
    if (!getToken()) {
      router.replace("/login");
      return;
    }
    api("/auth/me").then(setMe).catch(() => {});
  }, [router]);

  useEffect(() => setOpen(false), [path]);

  const isActive = (href: string) => (href === "/dashboard" ? path === href : path.startsWith(href));

  return (
    <div className="min-h-screen md:flex">
      <header className="flex items-center justify-between border-b border-line bg-white px-4 py-3 md:hidden">
        <span className="font-semibold">{me?.business?.name || "Duka"}</span>
        <button className="btn-ghost px-2 py-1" onClick={() => setOpen(!open)} aria-label="Menu">Menu</button>
      </header>
      <aside className={`${open ? "block" : "hidden"} w-full shrink-0 border-r border-line bg-white md:block md:min-h-screen md:w-56`}>
        <div className="hidden border-b border-line px-4 py-4 md:block">
          <div className="truncate font-semibold">{me?.business?.name || "…"}</div>
          <div className="truncate text-xs text-ink-mute">{me?.user?.email}</div>
        </div>
        <nav className="p-2">
          {NAV.map((n) => (
            <Link key={n.href} href={n.href}
              className={`block rounded-md px-3 py-2 text-sm ${isActive(n.href) ? "bg-brand-soft font-medium text-brand" : "text-ink-soft hover:bg-canvas"}`}>
              {n.label}
            </Link>
          ))}
          <button className="mt-4 block w-full rounded-md px-3 py-2 text-left text-sm text-ink-mute hover:bg-canvas"
            onClick={() => { setToken(null); router.push("/login"); }}>
            Sign out
          </button>
        </nav>
      </aside>
      <main className="min-w-0 flex-1 px-4 py-6 md:px-8">{children}</main>
    </div>
  );
}
