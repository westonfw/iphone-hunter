#!/usr/bin/env node
// 挂到已经开着远程调试口的 Chrome 上，把结账链路的每个请求/响应（含响应体）
// 逐条记到 jsonl，人操作、程序旁观。零依赖：用 Node 22+ 自带的 WebSocket，
// 走原生 CDP，不经 Playwright——两个 Playwright 客户端挂同一个端点会互相搅乱
// （README「用 Chrome 的 HAR，别用 hunter record」那一段），原生 CDP 的
// 浏览器级会话没有这个问题。
//
// 用法：node tools/cdp-record.mjs [--port 9222] [--out logs/live-record.jsonl]
//
// 隐私：请求头里的 cookie / authorization 不记；请求体只记结构性字段的值
// （门店、时段、步骤标记），其余字段只记长度。**响应体整段记**——search 里
// 的门店库存和时段就在里面，那是录这一遍的目的。pickupContact 那几步的响应
// 里可能带预填的姓名/手机，看完删文件。输出目录 logs/ 已在 .gitignore 里。

import { appendFileSync, mkdirSync } from "node:fs";
import { dirname } from "node:path";

const args = process.argv.slice(2);
const opt = (k, d) => { const i = args.indexOf(k); return i >= 0 ? args[i + 1] : d; };
const PORT = opt("--port", "9222");
const OUT = opt("--out", `logs/live-record-${new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-")}.jsonl`);
mkdirSync(dirname(OUT), { recursive: true });

const WATCH = ["/shop/checkoutx", "/shop/checkout", "/shop/bagx", "/shop/bag",
               "/shop/retail/pickup-message", "/shop/fulfillment-messages", "/shop/sba/"];
const STATIC = /\.(js|css|png|jpe?g|gif|svg|woff2?|ico|map)(\?|$)/i;
const SAFE_RE = /(^|\.)(_a|_m|_s|_n|step|storeNumber|selectStore|selectFulfillmentLocation|showAllStores|dayOfMonth|timeSlotValue|timeSlotId|startTime|endTime|date|isRecommended|city|state|district|countryCode|provinceCityDistrict|quantity|purchaseOption|selectBillingOption|selectInstallmentOption|locationConsent|selectBank|selectFapiao)$/;

const hhmmss = (d = new Date()) => d.toTimeString().slice(0, 8) + "." + String(d.getMilliseconds()).padStart(3, "0");
const write = (o) => appendFileSync(OUT, JSON.stringify(o) + "\n");
const say = (s) => console.log(`${hhmmss()} ${s}`);

function redactBody(raw) {
  if (!raw) return null;
  const out = [];
  try {
    if (raw.trim().startsWith("{")) {
      for (const [k, v] of Object.entries(JSON.parse(raw)))
        out.push(SAFE_RE.test(k) ? { k, v: String(v).slice(0, 60) } : { k, len: String(v).length });
      return out;
    }
    for (const [k, v] of new URLSearchParams(raw))
      out.push(SAFE_RE.test(k) ? { k, v: v.slice(0, 60) } : { k, len: v.length });
    return out;
  } catch { return [{ k: "<unparsed>", len: raw.length }]; }
}

// —— 响应体摘要：search / pickup-message 一眼看出门店库存和时段 ——
function* walk(o, key) {
  if (Array.isArray(o)) { for (const v of o) yield* walk(v, key); }
  else if (o && typeof o === "object") { if (key in o) yield o; for (const v of Object.values(o)) yield* walk(v, key); }
}
function summarize(url, text) {
  let d; try { d = JSON.parse(text); } catch { return ""; }
  if (url.includes("pickup-message")) {
    const stores = d?.body?.content?.pickupMessage?.stores || d?.body?.stores || [];
    return stores.slice(0, 12).map(s => {
      const pa = s.partsAvailability || {};
      return Object.entries(pa).map(([p, a]) => `${s.storeNumber}:${p.slice(0, 8)}=${a.pickupDisplay}`).join(" ");
    }).join(" ");
  }
  if (url.includes("fulfillment-messages")) {
    const parts = [];
    for (const n of walk(d, "storeNumber")) if (n.partsAvailability)
      for (const [p, a] of Object.entries(n.partsAvailability)) parts.push(`${n.storeNumber}:${p.slice(0, 8)}=${a.pickupDisplay}`);
    return parts.slice(0, 12).join(" ");
  }
  const stores = [];
  for (const n of walk(d, "retailStores")) for (const s of n.retailStores || []) {
    const av = s.availability || {};
    stores.push(`${s.storeId}${av.availableNowForAllLines ? "✓" : "✗"}${av.storeAvailability ? "(" + String(av.storeAvailability).slice(0, 8) + ")" : ""}`);
  }
  let slots = 0, dates = [];
  for (const n of walk(d, "timeSlotWindows")) {
    if (Array.isArray(n.pickUpDates)) dates = n.pickUpDates.map(x => x?.date).filter(Boolean);
    if (Array.isArray(n.timeSlotWindows)) for (const b of n.timeSlotWindows) if (b && typeof b === "object")
      for (const v of Object.values(b)) if (Array.isArray(v)) slots += v.filter(x => x && x.enabled !== false && x.timeSlotValue).length;
    break;
  }
  const head = d?.head || {};
  const dest = head?.data?.url ? ` → ${head.data.url}` : "";
  const bits = [];
  if (stores.length) bits.push("stores=" + stores.join(","));
  if (slots || dates.length) bits.push(`slots=${slots} dates=${dates.slice(0, 3).join("/")}`);
  if (head.status && head.status !== 200) bits.push(`head.status=${head.status}`);
  return bits.join(" ") + dest;
}

// —— CDP ——
const ver = await (await fetch(`http://127.0.0.1:${PORT}/json/version`)).json();
const ws = new WebSocket(ver.webSocketDebuggerUrl);
let seq = 0; const pending = new Map();
const send = (method, params = {}, sessionId) => new Promise((res, rej) => {
  const id = ++seq; pending.set(id, { res, rej });
  ws.send(JSON.stringify(sessionId ? { id, method, params, sessionId } : { id, method, params }));
});
await new Promise(r => ws.addEventListener("open", r));
say(`已挂到 ${ver.Browser}，记到 ${OUT}`);

const reqs = new Map();     // requestId → 记录
const sessions = new Map(); // sessionId → target url
const attached = new Set();  // targetId：autoAttach 和手动 attach 会各来一次，只挂一次
let lastAt = 0;

ws.addEventListener("message", async (ev) => {
  const m = JSON.parse(ev.data);
  if (m.id) { const p = pending.get(m.id); pending.delete(m.id); m.error ? p.rej(new Error(m.error.message)) : p.res(m.result); return; }
  const { method, params, sessionId } = m;
  try {
    if (method === "Target.attachedToTarget") {
      const { sessionId: sid, targetInfo } = params;
      if (targetInfo.type !== "page" || attached.has(targetInfo.targetId)) return;
      attached.add(targetInfo.targetId);
      sessions.set(sid, targetInfo.url);
      await send("Network.enable", { maxPostDataSize: 65536 }, sid);
      await send("Page.enable", {}, sid);
      await send("Runtime.runIfWaitingForDebugger", {}, sid).catch(() => {});
      say(`[tab] 已监听 ${targetInfo.url.slice(0, 100)}`);
      return;
    }
    if (method === "Target.detachedFromTarget") { sessions.delete(params.sessionId); return; }
    if (method === "Page.frameNavigated" && !params.frame.parentId) {
      say(`[nav] ${params.frame.url.slice(0, 120)}`);
      write({ t: hhmmss(), type: "nav", url: params.frame.url });
      return;
    }
    if (method === "Network.requestWillBeSent") {
      const { requestId, request, wallTime, redirectResponse } = params;
      const u = new URL(request.url);
      if (!u.hostname.endsWith("apple.com.cn") || !WATCH.some(k => u.pathname.startsWith(k)) || STATIC.test(u.pathname)) return;
      const q = u.searchParams;
      const rec = {
        t: hhmmss(new Date(wallTime * 1000)), wall: wallTime, type: "req", requestId, method: request.method,
        url: request.url.slice(0, 300), path: u.pathname.replace("/shop/", ""), a: q.get("_a") || "", m: q.get("_m") || "", s: q.get("_s") || "",
        stk: !!request.headers["x-aos-stk"] || !!request.headers["X-Aos-Stk"], body: redactBody(request.postData),
        redirectedFrom: redirectResponse ? redirectResponse.status : undefined,
      };
      reqs.set(requestId, rec);
      return;
    }
    if (method === "Network.responseReceived") {
      const rec = reqs.get(params.requestId); if (!rec) return;
      const r = params.response;
      rec.status = r.status; rec.mime = r.mimeType;
      rec.hdr = {}; for (const k of ["date", "age", "cache-control", "location", "x-cache", "server-timing"]) { const v = r.headers[k] ?? r.headers[k[0].toUpperCase() + k.slice(1)]; if (v) rec.hdr[k] = String(v).slice(0, 120); }
      rec.timing = r.timing ? { ttfbMs: Math.round(r.timing.receiveHeadersEnd - r.timing.sendEnd), queueMs: Math.round(r.timing.sendStart) } : undefined;
      return;
    }
    if (method === "Network.loadingFinished" || method === "Network.loadingFailed") {
      const rec = reqs.get(params.requestId); if (!rec) return; reqs.delete(params.requestId);
      rec.doneAt = hhmmss();
      if (method === "Network.loadingFailed") rec.error = params.errorText;
      else if (rec.status && rec.status !== 302 && rec.status !== 301 && rec.status !== 204) {
        try {
          const b = await send("Network.getResponseBody", { requestId: params.requestId }, sessionId);
          const text = b.base64Encoded ? Buffer.from(b.body, "base64").toString("utf8") : b.body;
          rec.bytes = text.length;
          rec.summary = summarize(rec.url, text);
          rec.respBody = text.length < 4_000_000 ? text : text.slice(0, 4_000_000);
        } catch (e) { rec.bodyErr = String(e.message).slice(0, 80); }
      }
      const now = Date.now(); const gap = lastAt ? `+${((now - lastAt) / 1000).toFixed(1)}s` : "      "; lastAt = now;
      const t0 = new Date(rec.wall * 1000).getTime(); const dur = ((now - t0) / 1000).toFixed(2);
      say(`${gap.padStart(7)} ${rec.method.padEnd(4)} ${String(rec.status ?? rec.error).padEnd(4)} ${dur.padStart(6)}s ${rec.path.slice(0, 30).padEnd(30)} ${(rec.a || rec.s || "").slice(0, 42).padEnd(42)} ${rec.summary || ""}`);
      const line = { ...rec }; delete line.respBody;
      write({ ...line, respBody: rec.respBody });
      return;
    }
  } catch (e) { say(`[err] ${method}: ${e.message}`); }
});

await send("Target.setDiscoverTargets", { discover: true });
await send("Target.setAutoAttach", { autoAttach: true, waitForDebuggerOnStart: false, flatten: true });
const { targetInfos } = await send("Target.getTargets");
for (const t of targetInfos) if (t.type === "page" && !attached.has(t.targetId)) await send("Target.attachToTarget", { targetId: t.targetId, flatten: true }).catch(e => say(`[tab] 挂不上 ${t.url.slice(0, 60)}：${e.message}`));
say("开始旁观。你在 Chrome 里正常操作，Ctrl+C 结束。");
process.on("SIGINT", () => { say(`结束，记录在 ${OUT}`); process.exit(0); });
