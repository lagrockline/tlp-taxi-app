/**
 * Worker Cloudflare — mini-backend pour les notifications push de l'app
 * "LDE Vols — Infos taxis".
 *
 * Rôle : GitHub Pages est un hébergement 100% statique, il ne peut pas
 * recevoir de requêtes d'inscription (abonnement push) envoyées par les
 * téléphones. Ce Worker joue ce rôle minimal :
 *
 *   POST /subscribe      body: objet PushSubscription du navigateur
 *                         → enregistre/actualise l'abonnement
 *   POST /unsubscribe     body: { endpoint }
 *                         → supprime un abonnement (utilisé par send_push.py
 *                           quand un envoi échoue avec 404/410)
 *   GET  /subscriptions   header: X-Admin-Key: <secret>
 *                         → renvoie la liste des abonnements (JSON), utilisé
 *                           par le workflow GitHub Actions pour l'envoi
 *
 * Stockage : une seule entrée KV nommée "subscriptions", contenant un objet
 * JSON { "<hash-endpoint>": {...PushSubscription...}, ... }. Suffisant pour
 * quelques dizaines/centaines d'abonnés ; pas fait pour des volumes massifs.
 *
 * Déploiement : voir README.md à la racine du projet pour les étapes
 * (création du Worker, binding KV, variable ADMIN_KEY).
 */

const SUBSCRIPTIONS_KEY = "subscriptions";

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
};
