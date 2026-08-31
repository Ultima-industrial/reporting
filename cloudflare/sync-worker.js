/**
 * Cloudflare Worker: secure proxy for the "Sync now" button on the Ultima
 * Pulse dashboard (https://ultima-industrial.github.io/reporting/).
 *
 * The dashboard is a public static site — it can never hold a GitHub token
 * itself (anyone viewing the page could read it out of the JS and use it
 * for anything that token can do, not just refresh this dashboard). This
 * Worker holds the token as an encrypted secret instead, and exposes one
 * narrow, rate-limited endpoint that does exactly one thing: ask GitHub to
 * re-run the "Daily Dashboard Deploy" workflow.
 *
 * Required setup (done once, in the Cloudflare dashboard, not here):
 * - Secret `GITHUB_TOKEN1`: a fine-grained GitHub PAT scoped ONLY to the
 *   Ultima-industrial/reporting repo, with "Actions: Read and write" and
 *   nothing else. (Named GITHUB_TOKEN1, not GITHUB_TOKEN, because that name
 *   was already taken by an earlier, truncated/exposed token attempt.)
 * - KV namespace binding `SYNC_KV`: used to enforce the cooldown below so a
 *   public visitor can't spam GitHub Actions runs.
 */

const REPO = "Ultima-industrial/reporting";
const WORKFLOW_FILE = "daily_dashboard.yml";
const ALLOWED_ORIGIN = "https://ultima-industrial.github.io";
const COOLDOWN_SECONDS = 300; // 5 minutes between triggers, shared across all visitors

function corsHeaders() {
  return {
    "Access-Control-Allow-Origin": ALLOWED_ORIGIN,
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
  };
}

function json(body, status) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...corsHeaders() },
  });
}

export default {
  async fetch(request, env) {
    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: corsHeaders() });
    }
    if (request.method !== "POST") {
      return json({ ok: false, error: "method_not_allowed" }, 405);
    }

    const now = Date.now();
    const last = await env.SYNC_KV.get("last_trigger");
    if (last) {
      const elapsedMs = now - parseInt(last, 10);
      if (elapsedMs < COOLDOWN_SECONDS * 1000) {
        const retryAfterSeconds = Math.ceil((COOLDOWN_SECONDS * 1000 - elapsedMs) / 1000);
        return json({ ok: false, error: "cooldown", retryAfterSeconds }, 429);
      }
    }

    const ghResponse = await fetch(
      `https://api.github.com/repos/${REPO}/actions/workflows/${WORKFLOW_FILE}/dispatches`,
      {
        method: "POST",
        headers: {
          Authorization: `Bearer ${env.GITHUB_TOKEN1}`,
          Accept: "application/vnd.github+json",
          "User-Agent": "ultima-pulse-sync-worker",
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ ref: "main" }),
      }
    );

    if (!ghResponse.ok) {
      const detail = await ghResponse.text();
      return json({ ok: false, error: "github_api_error", status: ghResponse.status, detail }, 502);
    }

    await env.SYNC_KV.put("last_trigger", String(now));
    return json({ ok: true }, 200);
  },
};
