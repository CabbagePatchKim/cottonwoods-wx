import fs from "node:fs";
import path from "node:path";
// Local check of cloud/viewer_worker.js (Node 22+, no packages): fake bucket = this folder's data/, real RS256 Access tokens.
//   node tests/viewer_worker_check.mjs            (writes the pages to a temp folder to open in a browser)
import os from "node:os";
import { fileURLToPath } from "node:url";
const proj = process.argv[2] || path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const out = process.argv[3] || fs.mkdtempSync(path.join(os.tmpdir(), "cwx-viewer-"));
const W = await import(new URL("../cloud/viewer_worker.js", import.meta.url));
const files = {
  "data/reports/storms_latest.html": "data/reports/storms_latest.html",
  "data/reports/storms_latest.txt": "data/reports/storms_latest.txt",
  "data/reports/verified_latest.txt": "data/reports/verified_latest.txt",
  "data/reports/models_latest.txt": "data/reports/models_latest.txt",
  "data/logs/cloud/models_2026-10.log": "data/logs/cloud/models_2026-10.log",
  "data/logs/cloud/models_2026-11.log": "data/logs/cloud/models_2026-11.log",
};
const bucket = {
  async head(k) { const f = files[k] && path.join(proj, files[k]); if (!f || !fs.existsSync(f)) return null;
    const st = fs.statSync(f); return { size: st.size, uploaded: st.mtime }; },
  async get(k, opt = {}) { const h = await this.head(k); if (!h) return null; let buf = fs.readFileSync(path.join(proj, files[k]));
    if (opt.range?.suffix) buf = buf.subarray(Math.max(0, buf.length - opt.range.suffix));
    return { uploaded: h.uploaded, text: async () => new TextDecoder().decode(buf) }; },
};
// Access: real RS256 keys
const kp = await crypto.subtle.generateKey({ name: "RSASSA-PKCS1-v1_5", modulusLength: 2048, publicExponent: new Uint8Array([1,0,1]), hash: "SHA-256" }, true, ["sign","verify"]);
const jwk = { ...(await crypto.subtle.exportKey("jwk", kp.publicKey)), kid: "k1", alg: "RS256" };
const team = "testteam.cloudflareaccess.com", aud = "AUD123";
globalThis.fetch = async (u) => { if (u === `https://${team}/cdn-cgi/access/certs`) return new Response(JSON.stringify({ keys: [jwk] })); throw new Error("unexpected fetch " + u); };
const b64 = o => Buffer.from(typeof o === "string" ? o : JSON.stringify(o)).toString("base64url");
async function token(claims, kid = "k1", key = kp.privateKey) {
  const h = b64({ alg: "RS256", kid }), p = b64(claims);
  const sig = await crypto.subtle.sign("RSASSA-PKCS1-v1_5", key, new TextEncoder().encode(`${h}.${p}`));
  return `${h}.${p}.${Buffer.from(sig).toString("base64url")}`;
}
const env = { BUCKET: bucket, ACCESS_TEAM_DOMAIN: team, ACCESS_AUD: aud, ALLOWED_EMAILS: "owner@example.com" };
const exp = Math.floor(Date.now() / 1000) + 3600;
const good = await token({ aud: [aud], iss: `https://${team}`, exp, email: "Owner@example.com" });
const req = (p, t) => new Request("https://cwx-viewer.example.workers.dev" + p, { headers: t ? { "Cf-Access-Jwt-Assertion": t } : {} });
let fails = 0; const check = (c, m) => { console.log((c ? "ok   " : "FAIL ") + m); if (!c) fails++; };
// auth cases
const other = await crypto.subtle.generateKey({ name: "RSASSA-PKCS1-v1_5", modulusLength: 2048, publicExponent: new Uint8Array([1,0,1]), hash: "SHA-256" }, true, ["sign"]);
const cases = [
  ["no token", undefined, env], ["forged signature", await token({ aud: [aud], iss: `https://${team}`, exp, email: "owner@example.com" }, "k1", other.privateKey), env],
  ["wrong audience", await token({ aud: ["X"], iss: `https://${team}`, exp, email: "owner@example.com" }), env],
  ["expired", await token({ aud: [aud], iss: `https://${team}`, exp: 1000, email: "owner@example.com" }), env],
  ["other email", await token({ aud: [aud], iss: `https://${team}`, exp, email: "someone@example.com" }), env],
  ["wrong issuer", await token({ aud: [aud], iss: "https://evil.cloudflareaccess.com", exp, email: "owner@example.com" }), env],
  ["unconfigured worker", good, { BUCKET: bucket }], ["garbage", "abc.def", env],
];
for (const [name, t, e] of cases) { const r = await W.default.fetch(req("/verified", t), e); check(r.status === 403 && !(await r.text()).includes("Verified"), `403 for ${name}`); }
// pages
for (const p of ["/", "/storms", "/storms.txt", "/verified", "/models", "/log", "/log?i=3&wrap=1", "/nope"]) {
  const t0 = performance.now(); const r = await W.default.fetch(req(p, good), env); const body = await r.text(); const ms = (performance.now() - t0).toFixed(0);
  check(r.status === (p === "/nope" ? 404 : 200) && r.headers.get("Cache-Control").includes("no-store"), `${p} -> ${r.status}, ${body.length} bytes, ${ms} ms`);
  fs.writeFileSync(path.join(out, (p.replace(/[\/?=&.]/g, "_") || "_") + ".html"), body);
}
const home = fs.readFileSync(path.join(out, "_.html"), "utf8");
check(home.includes("Last pass") && home.includes("pill ok"), "home shows last pass with step pills");
const post = await W.default.fetch(new Request("https://x.workers.dev/", { method: "POST", headers: { "Cf-Access-Jwt-Assertion": good } }), env);
check(post.status === 405, "POST refused");
// parsePasses edge cases
const pp = W.parsePasses("partial tail line\n===== 2026-11-18 23:50 UTC code v1.11 run https://x/1\nhello\ndone: 1 archived, 0 failed, 3 skipped after 4 failed tries\nTraceback boom\n----- steps: models success, obs failure, feed skipped\n===== 2026-11-19 02:50 UTC code v1.11 run https://x/2\nstill going");
check(pp.length === 2 && !pp[0].finished && pp[1].steps[1].outcome === "failure" && pp[1].problems.length === 1, "parsePasses: newest first, unfinished pass, failure, problem lines, partial head dropped");
console.log(`pages written to ${out}`);
process.exit(fails ? 1 : 0);
