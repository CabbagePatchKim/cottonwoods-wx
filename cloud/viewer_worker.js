// Cloudflare Worker: the owner's PRIVATE phone viewer for the cloud passes (owner, 2026-11-18). Reads what every pass
// already saves in the R2 bucket (data/reports/*, data/logs/cloud/models_YYYY-MM.log): nothing in the pass changes.
// PRIVATE, NEVER PUBLIC: the verified report holds Alta Collins totals (rule: RECORD, NEVER PUBLISH). So every request
// must carry a valid Cloudflare Access login token, checked here too (fail closed: no settings -> 403), not only by
// Access in front: a request reaching the Worker some other way (an unprotected route) still gets nothing.
//
// Set up in the Cloudflare dashboard (no build step; paste this file):
//   1. Workers & Pages -> Create -> Worker "cwx-viewer" -> Edit code -> paste -> Deploy.
//   2. Settings -> Bindings -> Add -> R2 bucket: variable name BUCKET, the pass's bucket. (Read only is all it uses.)
//   3. Settings -> Domains & Routes -> workers.dev: turn on "Cloudflare Access" for it (or Zero Trust -> Access ->
//      Applications -> Add -> Self-hosted, the workers.dev host name). Policy: Allow, Include "Emails" = your email.
//      Login method: One-time PIN (a code is emailed to you; free for up to 50 users).
//   4. Copy the Access application's "Application Audience (AUD) Tag" and your team domain (Zero Trust -> Settings
//      -> Custom Pages: <team>.cloudflareaccess.com). Settings -> Variables and Secrets of the Worker:
//        ACCESS_TEAM_DOMAIN = <team>.cloudflareaccess.com     ACCESS_AUD = the AUD tag
//        ALLOWED_EMAILS     = your email (comma-separated if ever more than one)
//   5. Open the workers.dev URL on the phone, log in with the emailed code, "Add to Home Screen".

const REPORTS = {                                  // path -> bucket key (the pass writes these each run)
  "/storms": "data/reports/storms_latest.html",
  "/storms.txt": "data/reports/storms_latest.txt",
  "/verified": "data/reports/verified_latest.txt",
  "/models": "data/reports/models_latest.txt",
};
const TITLES = { "/storms": "Storms", "/storms.txt": "Storms (text)", "/verified": "Verified", "/models": "Models" };
const LOG_PREFIX = "data/logs/cloud/models_";      // + YYYY-MM.log, one per month (pass.yml "Start this pass's log")
const PASS_MARK = "===== ";                        // pass.yml: first line of every pass
const STEPS_MARK = "----- steps: ";                // pass.yml: "Log the step results", last line of a pass
const LOG_TAIL_BYTES = 500000;                     // ~13 passes of ~37 KB; the month's file reaches ~8 MB
const PASSES_SHOWN = 12;
const STALE_HOURS = 4;                             // the Worker trigger runs a pass every 3 h
const TZ = "America/Denver";
// A real failure, not a pass summary like "0 failed, 3 skipped after 4 failed tries"
const PROBLEM = /(FAILED|Traceback|Error\b|error:|Exception|HTTP [45]\d\d|timed out|\b[1-9]\d* (of \d+ )?(files )?failed(?! tries))/;

// ---------- Access login check (RS256 JWT from Cf-Access-Jwt-Assertion) ----------
let certCache = { at: 0, keys: [] };

function b64urlBytes(s) {
  const b = atob(s.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((s.length + 3) % 4));
  return Uint8Array.from(b, c => c.charCodeAt(0));
}

async function accessKeys(team, now, fetcher) {
  if (now - certCache.at < 3600e3 && certCache.keys.length) return certCache.keys;
  const r = await fetcher(`https://${team}/cdn-cgi/access/certs`);
  if (!r.ok) throw new Error(`Access certs: HTTP ${r.status}`);
  certCache = { at: now, keys: (await r.json()).keys || [] };
  return certCache.keys;
}

export async function checkAccess(request, env, now = Date.now(), fetcher = fetch) {
  const team = (env.ACCESS_TEAM_DOMAIN || "").trim(), aud = (env.ACCESS_AUD || "").trim();
  const allowed = (env.ALLOWED_EMAILS || "").split(",").map(e => e.trim().toLowerCase()).filter(Boolean);
  if (!team || !aud || !allowed.length) return { ok: false, why: "viewer not configured" };
  const token = request.headers.get("Cf-Access-Jwt-Assertion");
  if (!token) return { ok: false, why: "no Access login" };
  const parts = token.split(".");
  if (parts.length !== 3) return { ok: false, why: "bad token" };
  try {
    const header = JSON.parse(new TextDecoder().decode(b64urlBytes(parts[0])));
    const claims = JSON.parse(new TextDecoder().decode(b64urlBytes(parts[1])));
    if (header.alg !== "RS256") return { ok: false, why: "bad token" };
    const jwk = (await accessKeys(team, now, fetcher)).find(k => k.kid === header.kid);
    if (!jwk) return { ok: false, why: "unknown key" };
    const key = await crypto.subtle.importKey("jwk", jwk, { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" },
      false, ["verify"]);
    const good = await crypto.subtle.verify("RSASSA-PKCS1-v1_5", key, b64urlBytes(parts[2]),
      new TextEncoder().encode(`${parts[0]}.${parts[1]}`));
    if (!good) return { ok: false, why: "bad signature" };
    const auds = Array.isArray(claims.aud) ? claims.aud : [claims.aud];
    if (!auds.includes(aud)) return { ok: false, why: "wrong audience" };
    if (claims.iss !== `https://${team}`) return { ok: false, why: "wrong issuer" };
    if (!(claims.exp * 1000 > now)) return { ok: false, why: "expired" };
    if (!allowed.includes(String(claims.email || "").toLowerCase())) return { ok: false, why: "not allowed" };
    return { ok: true, email: claims.email };
  } catch (e) {
    return { ok: false, why: "bad token" };
  }
}

// ---------- the log ----------
function monthKey(d) {
  return `${LOG_PREFIX}${d.toISOString().slice(0, 7)}.log`;
}

async function logTail(bucket, now) {
  const d = new Date(now), prev = new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth() - 1, 15));
  let text = "";
  for (const key of [monthKey(prev), monthKey(d)]) {      // previous month first, so a new month still has history
    const head = await bucket.head(key);
    if (!head) continue;
    const obj = await bucket.get(key, head.size > LOG_TAIL_BYTES ? { range: { suffix: LOG_TAIL_BYTES } } : {});
    if (obj) text += "\n" + await obj.text();
  }
  return text;
}

export function parsePasses(text) {
  const chunks = ("\n" + text).split("\n" + PASS_MARK).slice(1);   // a cut-off first chunk has no mark: dropped
  return chunks.map(c => {
    const lines = c.split("\n");
    const m = lines[0].match(/^(\d{4}-\d\d-\d\d \d\d:\d\d) UTC code (\S+)(?: run (\S+))?/) || [];
    const stepLine = lines.find(l => l.startsWith(STEPS_MARK));
    const steps = stepLine ? stepLine.slice(STEPS_MARK.length).split(",").map(s => s.trim()).filter(Boolean)
      .map(s => { const [name, outcome] = s.split(/\s+/); return { name, outcome }; }) : [];
    return {
      started: m[1] ? new Date(m[1].replace(" ", "T") + ":00Z") : null, code: m[2] || "?", run: m[3] || "",
      steps, finished: Boolean(stepLine), body: lines.slice(1).join("\n"),
      problems: lines.slice(1).filter(l => PROBLEM.test(l) && !l.startsWith(STEPS_MARK)),
    };
  }).reverse();                                                    // newest first
}

// ---------- pages ----------
const esc = s => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

function mt(d) {
  return d ? d.toLocaleString("en-US", { timeZone: TZ, weekday: "short", month: "numeric", day: "numeric",
    hour: "2-digit", minute: "2-digit", hour12: false, timeZoneName: "short" }) : "?";
}

function ago(d, now) {
  const m = Math.round((now - d) / 60000);
  return m < 90 ? `${m} min ago` : `${(m / 60).toFixed(1)} h ago`;
}

const STYLE = `:root{--bg:#fbfbfa;--fg:#1d1d1b;--muted:#6b6a66;--line:#e2e0da;--card:#fff;--ok:#1f7a3a;--bad:#b3261e;
--warn:#8a5a00;--accent:#2b5c8a}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--fg:#ecebe7;--muted:#a3a19b;--line:#33322f;--card:#1f1f1d;
--ok:#6cc58a;--bad:#ff8a80;--warn:#e0b050;--accent:#8ab4e0}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,-apple-system,
"Segoe UI",sans-serif}main{max-width:900px;margin:0 auto;padding:12px 16px 40px}
nav.cwx{position:sticky;top:0;z-index:9;display:flex;gap:4px;overflow-x:auto;padding:8px 12px;background:var(--card);
border-bottom:1px solid var(--line);font:600 14px system-ui,sans-serif}nav.cwx a{padding:6px 10px;border-radius:6px;
color:var(--accent);text-decoration:none;white-space:nowrap}nav.cwx a.on{background:var(--line);color:var(--fg)}
h1{font-size:20px;margin:8px 0}h2{font-size:16px;margin:22px 0 6px}a{color:var(--accent)}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin:10px 0}
.muted{color:var(--muted)}.small{font-size:13px}.ok{color:var(--ok)}.bad{color:var(--bad)}.warn{color:var(--warn)}
.pill{display:inline-block;margin:3px 4px 0 0;padding:2px 8px;border-radius:12px;font-size:13px;border:1px solid
var(--line)}.pill.ok{border-color:var(--ok)}.pill.bad{border-color:var(--bad);font-weight:600}
.pill.skip{opacity:.6}table{border-collapse:collapse;width:100%}td{padding:6px 4px;border-bottom:1px solid
var(--line);vertical-align:top;font-size:14px}pre{font:12px/1.35 ui-monospace,Menlo,Consolas,monospace;
overflow-x:auto;background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px;margin:8px 0}
pre.wrap{white-space:pre-wrap;word-break:break-word}pre .h{font-weight:700;color:var(--accent)}
pre .p{color:var(--bad);font-weight:600}details summary{cursor:pointer;color:var(--accent);margin:6px 0}`;

function nav(path) {
  const items = [["/", "Passes"], ["/storms", "Storms"], ["/verified", "Verified"], ["/models", "Models"],
    ["/log", "Log"], ["/storms.txt", "Storms text"]];
  return `<nav class="cwx">${items.map(([p, t]) =>
    `<a href="${p}"${p === path ? ' class="on"' : ""}>${t}</a>`).join("")}</nav>`;
}

function page(path, title, body) {
  return `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport"
content="width=device-width,initial-scale=1"><meta name="robots" content="noindex"><title>${esc(title)} · CWX</title>
<style>${STYLE}</style></head><body>${nav(path)}<main>${body}</main></body></html>`;
}

function stepPills(p) {
  if (!p.finished) return `<span class="pill warn">no step results: still running, or the pass died</span>`;
  return p.steps.map(s => {
    const cls = s.outcome === "success" ? "ok" : s.outcome === "failure" ? "bad" : "skip";
    return `<span class="pill ${cls}">${esc(s.name)} ${s.outcome === "success" ? "✓" : esc(s.outcome)}</span>`;
  }).join("");
}

async function homePage(env, now) {
  const passes = parsePasses(await logTail(env.BUCKET, now));
  const last = passes[0];
  let body = `<h1>Cloud passes</h1>`;
  if (!last) return page("/", "Passes", body + `<p class="bad">No pass log found in the bucket.</p>`);
  const age = (now - last.started) / 3600e3;
  const failed = last.steps.filter(s => s.outcome === "failure").map(s => s.name);
  const status = !last.finished ? `<span class="warn">running or died</span>`
    : failed.length ? `<span class="bad">FAILED: ${esc(failed.join(", "))}</span>` : `<span class="ok">all steps ok</span>`;
  body += `<div class="card"><div><b>Last pass</b> ${esc(mt(last.started))} <span class="muted">(${ago(last.started, now)})</span></div>
<div>${status} · code ${esc(last.code)}${age > STALE_HOURS ? ` · <span class="bad">no pass for ${age.toFixed(1)} h: check the trigger Worker's Logs</span>` : ""}</div>
<div>${stepPills(last)}</div>
${last.problems.length ? `<details><summary>${last.problems.length} problem line(s)</summary><pre class="wrap">${last.problems.slice(0, 40).map(esc).join("\n")}</pre></details>` : ""}
<div class="small"><a href="/log">full log of this pass</a>${last.run ? ` · <a href="${esc(last.run)}">GitHub run</a>` : ""}</div></div>`;
  body += `<h2>Reports</h2><div class="card"><table>`;
  for (const [p, key] of Object.entries(REPORTS)) {
    const h = await env.BUCKET.head(key);
    body += `<tr><td><a href="${p}">${TITLES[p]}</a></td><td class="muted small">${h ? `${esc(mt(h.uploaded))} · ${ago(h.uploaded, now)}` : "missing"}</td></tr>`;
  }
  body += `</table></div><h2>Recent passes</h2><div class="card"><table>`;
  passes.slice(0, PASSES_SHOWN).forEach((p, i) => {
    const bad = p.steps.filter(s => s.outcome === "failure").map(s => s.name);
    body += `<tr><td><a href="/log?i=${i}">${esc(mt(p.started))}</a><div class="muted small">${esc(p.code)}</div></td>
<td>${!p.finished ? `<span class="warn">unfinished</span>` : bad.length ? `<span class="bad">${esc(bad.join(", "))}</span>` : `<span class="ok">ok</span>`}</td></tr>`;
  });
  return page("/", "Passes", body + `</table></div>`);
}

function textBody(text, wrap, problems) {
  const lines = text.split("\n");
  const toc = [];
  const html = lines.map((l, n) => {
    if (/^(===|--- Storm)/.test(l)) {
      toc.push([n, l.replace(/^[=\- ]+/, "").slice(0, 70)]);
      return `<span class="h" id="s${n}">${esc(l)}</span>`;
    }
    return problems && PROBLEM.test(l) ? `<span class="p">${esc(l)}</span>` : esc(l);
  }).join("\n");
  const jump = toc.length > 1 ? `<details><summary>Sections (${toc.length})</summary>${toc.map(([n, t]) =>
    `<div><a href="#s${n}">${esc(t)}</a></div>`).join("")}</details>` : "";
  return `${jump}<pre${wrap ? ' class="wrap"' : ""}>${html}</pre>`;
}

function wrapToggle(url, wrap) {
  const u = new URL(url);
  wrap ? u.searchParams.delete("wrap") : u.searchParams.set("wrap", "1");
  return `<a class="small" href="${esc(u.pathname + u.search)}">${wrap ? "keep columns (scroll sideways)" : "wrap long lines"}</a>`;
}

async function reportPage(env, path, url, now) {
  const obj = await env.BUCKET.get(REPORTS[path]);
  if (!obj) return page(path, TITLES[path], `<h1>${TITLES[path]}</h1><p class="bad">Not in the bucket yet.</p>`);
  const text = await obj.text();
  if (path === "/storms") {                          // the pass's own HTML page, with the viewer's menu on top
    return text.replace(/<\/head>/i, `<style>${STYLE.match(/nav\.cwx[\s\S]*?\.on\{[^}]*\}/)[0]}</style></head>`)
      .replace(/<body>/i, `<body>${nav(path)}`);
  }
  const wrap = url.searchParams.has("wrap");
  return page(path, TITLES[path], `<h1>${TITLES[path]}</h1><div class="muted small">saved ${esc(mt(obj.uploaded))} (${ago(obj.uploaded, now)}) · ${wrapToggle(url, wrap)}</div>${textBody(text, wrap, false)}`);
}

async function logPage(env, url, now) {
  const passes = parsePasses(await logTail(env.BUCKET, now));
  const i = Math.max(0, Math.min(passes.length - 1, parseInt(url.searchParams.get("i") || "0", 10) || 0));
  const p = passes[i];
  if (!p) return page("/log", "Log", `<h1>Log</h1><p class="bad">No pass log found.</p>`);
  const wrap = url.searchParams.has("wrap");
  const older = i + 1 < passes.length ? `<a href="/log?i=${i + 1}${wrap ? "&wrap=1" : ""}">◀ older</a>` : "";
  const newer = i > 0 ? `<a href="/log?i=${i - 1}${wrap ? "&wrap=1" : ""}">newer ▶</a>` : "";
  return page("/log", "Log", `<h1>Pass ${esc(mt(p.started))}</h1><div class="small">code ${esc(p.code)}${p.run ? ` · <a href="${esc(p.run)}">GitHub run</a>` : ""}</div>
<div>${stepPills(p)}</div><div class="small" style="margin-top:6px">${older} ${newer} · ${wrapToggle(url, wrap)}</div>
${p.problems.length ? `<div class="card"><b class="bad">${p.problems.length} problem line(s)</b> (highlighted below)<pre class="wrap">${p.problems.slice(0, 40).map(esc).join("\n")}</pre></div>` : ""}
${textBody(p.body, wrap, true)}`);
}

const SECURITY = {
  "Cache-Control": "private, no-store",
  "X-Robots-Tag": "noindex, nofollow",
  "Referrer-Policy": "no-referrer",
  "X-Content-Type-Options": "nosniff",
  "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; img-src data:; frame-ancestors 'none'",
};

function respond(body, status = 200) {
  return new Response(body, { status, headers: { "Content-Type": "text/html; charset=utf-8", ...SECURITY } });
}

export default {
  async fetch(request, env) {
    const auth = await checkAccess(request, env);
    if (!auth.ok) return new Response(`Forbidden (${auth.why})`, { status: 403, headers: SECURITY });
    if (request.method !== "GET") return new Response("Method not allowed", { status: 405, headers: SECURITY });
    const url = new URL(request.url), now = Date.now();
    try {
      if (url.pathname === "/") return respond(await homePage(env, now));
      if (url.pathname === "/log") return respond(await logPage(env, url, now));
      if (REPORTS[url.pathname]) return respond(await reportPage(env, url.pathname, url, now));
      return respond(page("", "Not found", `<h1>Not found</h1><p><a href="/">Passes</a></p>`), 404);
    } catch (e) {
      console.log(`viewer error: ${e.stack || e}`);
      return respond(page("", "Error", `<h1>Something went wrong</h1><p class="muted">See the Worker's Logs tab.</p>`), 500);
    }
  },
};
