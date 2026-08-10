#!/usr/bin/env python3
"""
Envoie une notification push (Web Push standard) à tous les téléphones
abonnés, pour chaque vol qui vient de passer en retard ou en avance.

Lit :
  - data/_pending_alerts.json  (écrit par scrape.py à chaque exécution)

Récupère la liste des abonnés auprès du Cloudflare Worker (voir worker/),
qui joue le rôle de mini-backend pour stocker les abonnements créés côté
navigateur (GitHub Pages ne peut pas recevoir de requêtes d'inscription).

Variables d'environnement requises (à définir comme secrets GitHub Actions) :
  VAPID_PRIVATE_KEY   clé privée VAPID (format urlsafe base64, sans padding)
  VAPID_PUBLIC_KEY    clé publique VAPID (doit correspondre à celle utilisée
                       côté client dans index.html)
  VAPID_CLAIMS_EMAIL  ex: "mailto:contact@example.com" (requis par la norme,
                       sert de contact en cas d'abus signalé par un
                       fournisseur push)
  WORKER_BASE_URL     ex: "https://tlp-taxi-push.<votre-compte>.workers.dev"
  WORKER_ADMIN_KEY    clé secrète partagée avec le Worker pour lire la liste
                       des abonnements (route protégée)

Si ces variables ne sont pas définies, le script s'arrête proprement sans
erreur (utile pour laisser le workflow fonctionner même avant d'avoir
configuré le Worker/les clés).
"""

import json
import os
import sys
from pathlib import Path

import requests
from pywebpush import webpush, WebPushException

ALERTS_PATH = Path(__file__).resolve().parent.parent / "data" / "_pending_alerts.json"

REQUIRED_ENV = [
    "VAPID_PRIVATE_KEY",
    "VAPID_PUBLIC_KEY",
    "VAPID_CLAIMS_EMAIL",
    "WORKER_BASE_URL",
    "WORKER_ADMIN_KEY",
]


def load_alerts() -> list[dict]:
    if not ALERTS_PATH.exists():
        return []
    try:
        return json.loads(ALERTS_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []


def fetch_subscriptions(worker_base_url: str, admin_key: str) -> list[dict]:
    resp = requests.get(
        f"{worker_base_url.rstrip('/')}/subscriptions",
        headers={"X-Admin-Key": admin_key},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def notify_expired_subscription(worker_base_url: str, admin_key: str, endpoint: str) -> None:
    """Prévient le Worker qu'un abonnement n'est plus valide, pour qu'il le
    retire de la liste (évite d'accumuler des envois voués à échouer)."""
    try:
        requests.post(
            f"{worker_base_url.rstrip('/')}/unsubscribe",
            headers={"X-Admin-Key": admin_key, "Content-Type": "application/json"},
            json={"endpoint": endpoint},
            timeout=10,
        )
    except requests.RequestException:
        pass  # best-effort, on ne bloque pas le job pour ça


def build_notification_payload(alert: dict) -> dict:
    flight = alert["flight"]
    label = "Départ" if alert["type"] == "departures" else "Arrivée"
    if alert["kind"] == "retarde":
        title = f"⚠️ {label} retardé — {flight['destination']}"
    else:
        title = f"⏱️ {label} en avance — {flight['destination']}"

    airline = flight.get("airline") or ""
    flight_number = flight.get("flight_number") or ""
    status_raw = flight.get("status_raw") or ""
    body = f"{airline} {flight_number} · {flight['time']} · {status_raw}".strip()

    return {
        "title": title,
        "body": body,
        "tag": f"{alert['type']}-{flight['destination']}-{flight['time']}",
    }


def main() -> int:
    missing = [name for name in REQUIRED_ENV if not os.environ.get(name)]
    if missing:
        print(
            "Notifications push non configurées (variables manquantes : "
            f"{', '.join(missing)}) — étape ignorée.",
        )
        return 0

    alerts = load_alerts()
    if not alerts:
        print("Aucune alerte à notifier.")
        return 0

    vapid_private_key = os.environ["VAPID_PRIVATE_KEY"]
    vapid_claims_email = os.environ["VAPID_CLAIMS_EMAIL"]
    worker_base_url = os.environ["WORKER_BASE_URL"]
    worker_admin_key = os.environ["WORKER_ADMIN_KEY"]

    try:
        subscriptions = fetch_subscriptions(worker_base_url, worker_admin_key)
    except requests.RequestException as exc:
        print(f"ERREUR: impossible de récupérer les abonnements : {exc}", file=sys.stderr)
        return 1

    if not subscriptions:
        print("Aucun téléphone abonné pour le moment.")
        return 0

    sent, failed, expired = 0, 0, 0

    for alert in alerts:
        payload = build_notification_payload(alert)
        for sub in subscriptions:
            try:
                webpush(
                    subscription_info=sub,
                    data=json.dumps(payload, ensure_ascii=False),
                    vapid_private_key=vapid_private_key,
                    vapid_claims={"sub": vapid_claims_email},
                )
                sent += 1
            except WebPushException as exc:
                status = getattr(exc.response, "status_code", None)
                if status in (404, 410):
                    # Abonnement expiré (app désinstallée, permission retirée...)
                    expired += 1
                    notify_expired_subscription(
                        worker_base_url, worker_admin_key, sub.get("endpoint", "")
                    )
                else:
                    failed += 1
                    print(f"Échec d'envoi : {exc}", file=sys.stderr)

    print(f"Notifications envoyées : {sent} | échecs : {failed} | abonnements expirés retirés : {expired}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
