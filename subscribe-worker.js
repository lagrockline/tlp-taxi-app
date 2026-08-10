/**
 * Worker Cloudflare — mini-backend pour les notifications push de l'app
 * "LDE Vols — Infos taxis", ET horloge fiable pour déclencher le scraping.
 *
 * DEUX RÔLES DISTINCTS DANS CE FICHIER :
 *
 * 1) fetch() : stockage des abonnements push (voir routes plus bas).
 *
 * 2) scheduled() : le "vrai" déclencheur toutes les 5 minutes. Le cron natif
 *    de GitHub Actions (`schedule:` dans le workflow) n'est PAS fiable à
 *    haute fréquence — GitHub le documente lui-même et repousse ces
 *    exécutions de façon importante en cas de charge (constaté : des écarts
 *    de 30 à 140 minutes au lieu de 5). Les "Cron Triggers" de Cloudflare
 *    Workers sont beaucoup plus précis. On les utilise donc comme horloge
 *    externe fiable, qui se contente d'appeler l'API GitHub pour déclencher
 *    le workflow (lequel s'exécute quasi instantanément une fois sollicité,
 *    comme observé sur les runs "Manually run").
 *
 * Configuration nécessaire (voir README) :
 *   - Secret GITHUB_DISPATCH_TOKEN : Personal Access Token GitHub (fine-
 *     grained, scope Actions: Read and write, limité à ce dépôt)
 *   - Variables GITHUB_OWNER / GITHUB_REPO / GITHUB_WORKFLOW_FILE
 *   - Cron Trigger configuré dans wrangler.toml : toutes les 5 minutes
 */

const SUBSCRIPTIONS_KEY = "subscriptions";

async function triggerGithubWorkflow(env) {
  const owner = env.GITHUB_OWNER;
  const repo = env.GITHUB_REPO;
  const workflowFile = env.GITHUB_WORKFLOW_FILE || "update-flights.yml";

  if (!env.GITHUB_DISPATCH_TOKEN || !owner || !repo) {
    console.log("Déclenchement GitHub ignoré : configuration manquante (secret/variables).");
    return;
  }

  const url = `https://api.github.com/repos/${owner}/${repo}/actions/workflows/${workflowFile}/dispatches`;

  const res = await fetch(url, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${env.GITHUB_DISPATCH_TOKEN}`,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "tlp-taxi-push-worker",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ ref: "main" }),
  });

  if (!res.ok) {
    const text = await res.text();
    console.log(`Échec du déclenchement GitHub (${res.status}) : ${text}`);
  }
}

function corsHeaders(origin) {
  return {
    "Access-Control-Allow-Origin": origin || "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, X-Admin-Key",
  };
}

async function readSubscriptions(env) {
  const raw = await env.SUBSCRIPTIONS_KV.get(SUBSCRIPTIONS_KEY);
  return raw ? JSON.parse(raw) : {};
}

async function writeSubscriptions(env, subs) {
  await env.SUBSCRIPTIONS_KV.put(SUBSCRIPTIONS_KEY, JSON.stringify(subs));
}

async function hashEndpoint(endpoint) {
  const encoder = new TextEncoder();
  const data = encoder.encode(endpoint);
  const digest = await crypto.subtle.digest("SHA-256", data);
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const origin = request.headers.get("Origin");

    if (request.method === "OPTIONS") {
      return new Response(null, { headers: corsHeaders(origin) });
    }

    // ---- POST /subscribe ----
    if (url.pathname === "/subscribe" && request.method === "POST") {
      let body;
      try {
        body = await request.json();
      } catch {
        return new Response("JSON invalide", { status: 400, headers: corsHeaders(origin) });
      }
      if (!body || !body.endpoint) {
        return new Response("Abonnement invalide", { status: 400, headers: corsHeaders(origin) });
      }

      const subs = await readSubscriptions(env);
      const key = await hashEndpoint(body.endpoint);
      subs[key] = body;
      await writeSubscriptions(env, subs);

      return new Response(JSON.stringify({ ok: true }), {
        status: 200,
        headers: { "Content-Type": "application/json", ...corsHeaders(origin) },
      });
    }

    // ---- POST /unsubscribe ----
    if (url.pathname === "/unsubscribe" && request.method === "POST") {
      // Protégé par la clé admin car appelé uniquement depuis le workflow
      // GitHub Actions (l'app cliente n'a pas besoin de désabonner autrui).
      const adminKey = request.headers.get("X-Admin-Key");
      if (!env.ADMIN_KEY || adminKey !== env.ADMIN_KEY) {
        return new Response("Non autorisé", { status: 401, headers: corsHeaders(origin) });
      }
      let body;
      try {
        body = await request.json();
      } catch {
        return new Response("JSON invalide", { status: 400, headers: corsHeaders(origin) });
      }
      if (!body || !body.endpoint) {
        return new Response("endpoint manquant", { status: 400, headers: corsHeaders(origin) });
      }

      const subs = await readSubscriptions(env);
      const key = await hashEndpoint(body.endpoint);
      delete subs[key];
      await writeSubscriptions(env, subs);

      return new Response(JSON.stringify({ ok: true }), {
        status: 200,
        headers: { "Content-Type": "application/json", ...corsHeaders(origin) },
      });
    }

    // ---- GET /subscriptions ----
    if (url.pathname === "/subscriptions" && request.method === "GET") {
      const adminKey = request.headers.get("X-Admin-Key");
      if (!env.ADMIN_KEY || adminKey !== env.ADMIN_KEY) {
        return new Response("Non autorisé", { status: 401, headers: corsHeaders(origin) });
      }
      const subs = await readSubscriptions(env);
      return new Response(JSON.stringify(Object.values(subs)), {
        status: 200,
        headers: { "Content-Type": "application/json", ...corsHeaders(origin) },
      });
    }

    return new Response("Not found", { status: 404, headers: corsHeaders(origin) });
  },

  // Appelé automatiquement par Cloudflare selon le(s) Cron Trigger(s)
  // défini(s) dans wrangler.toml (ex: "*/5 * * * *"). C'est cette horloge,
  // fiable, qui remplace le `schedule:` peu fiable de GitHub Actions.
  async scheduled(event, env, ctx) {
    ctx.waitUntil(triggerGithubWorkflow(env));
  },
};

