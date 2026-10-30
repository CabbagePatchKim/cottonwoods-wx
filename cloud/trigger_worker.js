// Cloudflare Worker that starts the GitHub Actions model pass on time: the ONLY trigger (owner, 2026-10-29).
// GitHub's own schedule dropped about half its slots and ran the rest up to ~3 h late, so it was removed from
// pass.yml. This presses the workflow's "Run workflow" button every 3 hours; if a pass is already queued or
// running, or started in the last 30 min (e.g. one started by hand), it does nothing.
//
// Set up in the Cloudflare dashboard (no build step; paste this file):
//   Workers & Pages -> Create -> Worker "cwx-pass-trigger" -> Edit code -> paste -> Deploy.
//   Settings -> Variables and Secrets -> add Secret GITHUB_TOKEN: a fine-grained GitHub token, repository
//     CabbagePatchKim/cottonwoods-wx only, permission "Actions: Read and write" (nothing else).
//   Settings -> Trigger events -> Cron trigger: the CRON below (UTC).
//   Settings -> Domains & Routes: the workers.dev URL can be disabled; this Worker only runs on its schedule.
// Logs: the Worker's "Logs" tab (or Observability) shows one line per trigger.

const CRON = "50 2,5,8,11,14,17,20,23 * * *";   // every 3 h at :50 UTC
const REPO = "CabbagePatchKim/cottonwoods-wx";
const WORKFLOW = "pass.yml";
const RECENT_MINUTES = 30;

async function github(env, path, init = {}) {
  return fetch(`https://api.github.com/repos/${REPO}/actions/workflows/${WORKFLOW}${path}`, {
    ...init,
    headers: {
      "Authorization": `Bearer ${env.GITHUB_TOKEN}`,
      "Accept": "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "cwx-pass-trigger",
      ...(init.body ? { "Content-Type": "application/json" } : {}),
    },
  });
}

async function trigger(env, now) {
  const listed = await github(env, "/runs?per_page=5");
  if (!listed.ok) throw new Error(`listing runs failed: HTTP ${listed.status} ${await listed.text()}`);
  const runs = (await listed.json()).workflow_runs;
  const busy = runs.find(r => r.status !== "completed"
    || now - Date.parse(r.created_at) < RECENT_MINUTES * 60 * 1000);
  if (busy) {
    console.log(`skip: run ${busy.run_number} (${busy.event}, ${busy.status}) created ${busy.created_at}`);
    return;
  }
  const sent = await github(env, "/dispatches", { method: "POST", body: JSON.stringify({ ref: "main" }) });
  if (sent.status !== 204) throw new Error(`dispatch failed: HTTP ${sent.status} ${await sent.text()}`);
  console.log(`started a pass (no run in the last ${RECENT_MINUTES} min)`);
}

export default {
  async scheduled(event, env, ctx) {
    ctx.waitUntil(trigger(env, event.scheduledTime));
  },
};
