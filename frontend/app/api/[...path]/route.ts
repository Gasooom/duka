// Runtime proxy: the browser calls same-origin /api/*, this forwards to the FastAPI backend.
// Keeps the backend URL server-side and avoids CORS; no secrets are ever shipped to the browser.
import { NextRequest } from "next/server";

// A full URL (docker compose: http://backend:8000), or the bare host:port that render.yaml passes for the API on
// Render's private network (fromService hostport), where services talk plain http.
const configured = process.env.BACKEND_URL || "http://localhost:8000";
const BACKEND = /^https?:\/\//i.test(configured) ? configured : `http://${configured}`;

async function proxy(req: NextRequest, ctx: { params: Promise<{ path: string[] }> }) {
  const { path } = await ctx.params;
  const url = `${BACKEND}/api/${path.map(encodeURIComponent).join("/")}${req.nextUrl.search}`;
  const headers = new Headers();
  for (const h of ["authorization", "content-type", "x-request-id"]) {
    const v = req.headers.get(h);
    if (v) headers.set(h, v);
  }
  const body = ["GET", "HEAD"].includes(req.method) ? undefined : await req.arrayBuffer();
  try {
    const res = await fetch(url, { method: req.method, headers, body, cache: "no-store" });
    const out = new Headers();
    const ct = res.headers.get("content-type");
    if (ct) out.set("content-type", ct);
    return new Response(res.status === 204 ? null : await res.arrayBuffer(), { status: res.status, headers: out });
  } catch {
    return Response.json({ detail: "Duka's server is not responding right now. Please try again in a minute." },
      { status: 502 });
  }
}

export { proxy as GET, proxy as POST, proxy as PATCH, proxy as PUT, proxy as DELETE };
