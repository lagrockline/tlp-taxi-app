#!/usr/bin/env python3
"""
Scraper pour la page "Infos vols du jour" de l'aéroport Tarbes-Lourdes-Pyrénées.

Principe : la page ne fournit pas d'API. On récupère le HTML, on en extrait le
texte visible dans l'ordre du document, puis on reconnaît les vols grâce au
format récurrent (constaté manuellement sur la page) :

    NOM DE LA DESTINATION (tout en majuscules)
    JJ/MM HH:MM
    COMPAGNIE ... N°VOL   [STATUT optionnel, ex: "Décollé 11h41", "Retardé", "Prévu 14h45", "Annulé"]

Cette approche par motif de texte est plus résiliente à une refonte graphique
du site qu'un ciblage par classes CSS, mais reste dépendante du format actuel
du texte. Si l'aéroport change la formulation, il faudra ajuster les regex
ci-dessous.

Sortie : data/flights.json avec la structure :
{
  "scraped_at": "2026-08-09T12:34:56+02:00",
  "source_url": "...",
  "departures": [ {...} ],
  "arrivals": [ {...} ]
}
"""

import json
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
from bs4 import BeautifulSoup

SOURCE_URL = "https://www.tlp.aeroport.fr/page/informations-vols-du-jour"
OUTPUT_PATH = Path(__file__).resolve().parent.parent / "data" / "flights.json"

# Paris est en UTC+1 (hiver) / UTC+2 (été) ; on stocke en heure locale Paris approx.
PARIS_TZ = timezone(timedelta(hours=2))

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; TLP-Taxi-App/1.0; "
        "+https://github.com/) infos-vols scraper pour chauffeurs de taxi"
    )
}

# Compagnies reconnues (utile pour le filtre côté appli)
AIRLINE_PATTERNS = [
    ("RYANAIR", "Ryanair"),
    ("VOLOTEA", "Volotea"),
    ("AIR FRANCE", "Air France"),
    ("EASYJET", "EasyJet"),
    ("TRANSAVIA", "Transavia"),
    ("VUELING", "Vueling"),
]

DATE_RE = re.compile(r"^\d{2}/\d{2}$")
TIME_RE = re.compile(r"^\d{2}:\d{2}$")
CITY_RE = re.compile(r"^[A-ZÀÂÄÉÈÊËÎÏÔÖÙÛÜÇ0-9'\-\s]+$")

STATUS_KEYWORDS = {
    "décollé": "decolle",
    "arrivé": "atterri",
    "atterri": "atterri",
    "prévu": "prevu",
    "retardé": "retarde",
    "en avance": "avance",
    "avancé": "avance",
    "annulé": "annule",
    "embarquement": "embarquement",
}


def fetch_html(url: str) -> str:
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    return resp.text


def extract_lines(html: str) -> list[str]:
    """Retourne le texte visible de la page, une ligne logique par élément.

    Le statut d'un vol (ex: "Décollé 11h41") est souvent dans un <strong>
    imbriqué dans le même paragraphe que la compagnie/n° de vol. BeautifulSoup
    peut le renvoyer comme un noeud de texte séparé : on le refusionne ici
    avec la ligne précédente pour retrouver le format "COMPAGNIE VOL STATUT"
    observé sur la page réelle.
    """
    soup = BeautifulSoup(html, "html.parser")

    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    raw_lines = [t.strip() for t in soup.stripped_strings if t.strip()]

    lines: list[str] = []
    for line in raw_lines:
        lower = line.lower()
        starts_with_status = any(lower.startswith(kw) for kw in STATUS_KEYWORDS)
        if starts_with_status and lines:
            lines[-1] = f"{lines[-1]} {line}"
        else:
            lines.append(line)
    return lines


def slice_section(lines: list[str], start_marker: str, end_markers: list[str]) -> list[str]:
    """Découpe la liste de lignes entre un marqueur de début et le premier
    marqueur de fin rencontré."""
    try:
        start = lines.index(start_marker) + 1
    except ValueError:
        return []

    end = len(lines)
    for marker in end_markers:
        try:
            idx = lines.index(marker, start)
            end = min(end, idx)
        except ValueError:
            continue
    return lines[start:end]


def parse_flight_block(lines: list[str]) -> list[dict]:
    """Reconnaît des groupes de 5 lignes dans la liste de lignes de texte :

        VILLE
        JJ/MM
        HH:MM
        COMPAGNIE
        N°VOL [STATUT optionnel, déjà fusionné par extract_lines]

    Ce motif a été identifié à partir du HTML réel de la page (voir
    discussion de debug). Le site duplique parfois la même entrée dans le
    HTML (probablement un artefact de mise en page desktop/mobile) : on
    dédoublonne en fin de fonction sur (destination, date, heure, compagnie).
    """
    flights = []
    i = 0
    n = len(lines)
    while i < n:
        city_line = lines[i]

        # La ligne "ville" doit ressembler à une destination (majuscules)
        # et ne pas être elle-même une ligne date ou heure.
        if not CITY_RE.match(city_line) or DATE_RE.match(city_line) or TIME_RE.match(city_line):
            i += 1
            continue

        if i + 4 >= n:
            i += 1
            continue

        date_line = lines[i + 1]
        time_line = lines[i + 2]
        airline_line = lines[i + 3]
        flight_line = lines[i + 4]

        if not DATE_RE.match(date_line) or not TIME_RE.match(time_line):
            i += 1
            continue

        airline_name = None
        for pattern, display_name in AIRLINE_PATTERNS:
            if pattern in airline_line.upper():
                airline_name = display_name
                break

        # Numéro de vol : premier token alphanumérique du type lettres+chiffres
        # en début de ligne (ex: "V72182", "FR521").
        flight_number = None
        flight_num_match = re.match(r"\s*([A-Z0-9]{2,3}\d{2,5}[A-Z]?)\b", flight_line)
        if flight_num_match:
            flight_number = flight_num_match.group(1)
            # On retire un éventuel suffixe "P" isolé collé au numéro (ex:
            # "V77784P"), qui correspond à un doublon de rendu plutôt qu'à
            # un vrai numéro de vol différent. Voir dédoublonnage plus bas.

        status_raw = None
        status_code = None
        delayed = False
        early = False
        for keyword, code in STATUS_KEYWORDS.items():
            if keyword in flight_line.lower():
                status_code = code
                idx = flight_line.lower().find(keyword)
                status_raw = flight_line[idx:].strip()
                delayed = code == "retarde"
                early = code == "avance"
                break

        # Clé de dédoublonnage : numéro de vol sans un éventuel "P" isolé en
        # toute fin (artefact de duplication constaté sur le site).
        dedupe_flight_number = flight_number
        if dedupe_flight_number and dedupe_flight_number.endswith("P") and len(dedupe_flight_number) > 1:
            without_p = dedupe_flight_number[:-1]
            if re.match(r"^[A-Z0-9]{2,3}\d{2,5}$", without_p):
                dedupe_flight_number = without_p

        flights.append(
            {
                "destination": city_line.strip(),
                "date": date_line,
                "time": time_line,
                "airline": airline_name,
                "airline_raw": airline_line,
                "flight_number": flight_number,
                "status_code": status_code,
                "status_raw": status_raw,
                "delayed": delayed,
                "early": early,
                "_dedupe_key": (city_line.strip(), date_line, time_line, airline_name, dedupe_flight_number),
            }
        )
        i += 5

    # Dédoublonnage : on garde une seule entrée par clé, en préférant celle
    # qui porte un statut (plus d'information) si les autres n'en ont pas.
    deduped: dict[tuple, dict] = {}
    for f in flights:
        key = f.pop("_dedupe_key")
        if key not in deduped:
            deduped[key] = f
        elif not deduped[key].get("status_raw") and f.get("status_raw"):
            deduped[key] = f

    return list(deduped.values())


def build_payload(html: str) -> dict:
    lines = extract_lines(html)

    departures_lines = slice_section(
        lines,
        start_marker="Prochains départs",
        end_markers=["Prochaines arrivées"],
    )
    arrivals_lines = slice_section(
        lines,
        start_marker="Prochaines arrivées",
        end_markers=["Rejoignez-nous sur...", "Rejoignez-", "Rejoignez-nous"],
    )

    departures = parse_flight_block(departures_lines)
    arrivals = parse_flight_block(arrivals_lines)

    return {
        "scraped_at": datetime.now(tz=PARIS_TZ).isoformat(),
        "source_url": SOURCE_URL,
        "departures": departures,
        "arrivals": arrivals,
    }


def flight_key(flight_type: str, flight: dict) -> str:
    return "|".join(
        [
            flight_type,
            flight.get("destination", ""),
            flight.get("date", ""),
            flight.get("time", ""),
            flight.get("flight_number") or "",
        ]
    )


def compute_alerts(old_payload: dict | None, new_payload: dict) -> list[dict]:
    """Compare l'ancien et le nouveau relevé de vols et retourne la liste des
    vols qui viennent de passer en retard ou en avance (nouveau changement,
    pas déjà signalé au tour précédent)."""
    if not old_payload:
        return []

    old_status = {}
    for flight_type in ("departures", "arrivals"):
        for f in old_payload.get(flight_type, []):
            old_status[flight_key(flight_type, f)] = f.get("status_code")

    alerts = []
    for flight_type in ("departures", "arrivals"):
        for f in new_payload.get(flight_type, []):
            key = flight_key(flight_type, f)
            prev_status = old_status.get(key)
            new_status = f.get("status_code")
            if new_status == "retarde" and prev_status != "retarde":
                alerts.append({"type": flight_type, "kind": "retarde", "flight": f})
            elif new_status == "avance" and prev_status != "avance":
                alerts.append({"type": flight_type, "kind": "avance", "flight": f})

    return alerts


def main() -> int:
    try:
        html = fetch_html(SOURCE_URL)
    except requests.RequestException as exc:
        print(f"ERREUR: impossible de récupérer la page source : {exc}", file=sys.stderr)
        return 1

    payload = build_payload(html)

    if not payload["departures"] and not payload["arrivals"]:
        # On évite d'écraser un JSON valide précédent avec un résultat vide,
        # ce qui indiquerait probablement que la page a changé de structure
        # — ou que son contenu est chargé en JavaScript après coup (dans ce
        # cas, `requests` ne voit qu'une coquille HTML vide).
        print(
            "ATTENTION: aucun vol détecté — la structure de la page a peut-être "
            "changé, ou son contenu est chargé dynamiquement en JavaScript.",
            file=sys.stderr,
        )
        print(f"Taille du HTML récupéré : {len(html)} caractères", file=sys.stderr)
        print("--- Premiers 1000 caractères du HTML reçu ---", file=sys.stderr)
        print(html[:1000], file=sys.stderr)
        print("--- Fin de l'extrait ---", file=sys.stderr)

        lines_debug = extract_lines(html)
        print(f"Nombre de lignes de texte extraites : {len(lines_debug)}", file=sys.stderr)
        print("--- Premières 40 lignes de texte extraites ---", file=sys.stderr)
        for l in lines_debug[:40]:
            print(f"  {l!r}", file=sys.stderr)
        print("--- Fin des lignes ---", file=sys.stderr)

        found_marker = "Prochains départs" in lines_debug
        print(f"'Prochains départs' trouvé dans le texte : {found_marker}", file=sys.stderr)

        if found_marker:
            idx = lines_debug.index("Prochains départs")
            print("--- 40 lignes autour du marqueur 'Prochains départs' ---", file=sys.stderr)
            for l in lines_debug[max(0, idx - 5):idx + 40]:
                print(f"  {l!r}", file=sys.stderr)
            print("--- Fin du contexte ---", file=sys.stderr)

        # Recherche d'indices d'un chargement AJAX (appel API séparé) dans le
        # HTML brut : URLs contenant "api", "vol", "flight", ou appels
        # fetch/ajax/axios visibles dans les <script> inline.
        print("--- Recherche d'indices d'appel AJAX/API dans le HTML brut ---", file=sys.stderr)
        candidate_patterns = [
            r'["\']([^"\']*(?:api|flight|vols?|json)[^"\']*)["\']',
        ]
        seen = set()
        for pattern in candidate_patterns:
            for m in re.finditer(pattern, html, re.IGNORECASE):
                val = m.group(1)
                if (
                    val not in seen
                    and 3 < len(val) < 150
                    and not val.startswith("data:")
                    and any(c.isalpha() for c in val)
                ):
                    seen.add(val)
        for val in sorted(seen)[:60]:
            print(f"  {val!r}", file=sys.stderr)
        print(f"--- Fin ({len(seen)} correspondances au total) ---", file=sys.stderr)

        return 2

    old_payload = None
    if OUTPUT_PATH.exists():
        try:
            old_payload = json.loads(OUTPUT_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            old_payload = None

    alerts = compute_alerts(old_payload, payload)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # Fichier éphémère (non commité) utilisé par send_push.py dans la même
    # exécution du workflow, pour savoir quelles notifications envoyer.
    alerts_path = OUTPUT_PATH.parent / "_pending_alerts.json"
    alerts_path.write_text(json.dumps(alerts, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"OK: {len(payload['departures'])} départs, {len(payload['arrivals'])} arrivées écrits dans {OUTPUT_PATH}")
    print(f"Alertes détectées : {len(alerts)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
