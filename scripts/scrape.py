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

import asyncio
import json
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

from playwright.async_api import async_playwright
from bs4 import BeautifulSoup

SOURCE_URL = "https://www.tlp.aeroport.fr/page/informations-vols-du-jour"
OUTPUT_PATH = Path(__file__).resolve().parent.parent / "data" / "flights.json"
DEBUG_PATH = Path(__file__).resolve().parent.parent / "data" / "_debug_page.html"

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


# Nouvelles variables JS signalées par le site (identifiées dans les logs CI) :
# tVolsDep, volsArr, volsDep
# On les supporte explicitement comme source primaire, car le DOM visible n'est
# plus toujours aux mêmes emplacements.


def normalize_status(raw_value: str | None) -> tuple[str | None, str | None, bool]:
    if raw_value is None:
        return None, None, False

    value = str(raw_value).strip().lower()
    for keyword, code in STATUS_KEYWORDS.items():
        if keyword in value:
            return code, raw_value.strip(), code == "retarde"
    return None, None, False


def _first_present(mapping: dict, *keys: str):
    for key in keys:
        if key in mapping and mapping[key] not in (None, ""):
            return mapping[key]
    for key in keys:
        if key.lower() in mapping and mapping[key.lower()] not in (None, ""):
            return mapping[key.lower()]
    return None


def normalize_js_flights(raw_entries, *, kind: str) -> list[dict]:
    if raw_entries is None:
        return []
    if isinstance(raw_entries, dict):
        for candidate_key in ("items", "flights", "data", "vols", kind):
            if candidate_key in raw_entries:
                raw_entries = raw_entries[candidate_key]
                break

    if not isinstance(raw_entries, list):
        return []

    normalized = []
    for entry in raw_entries:
        if isinstance(entry, dict):
            flight = {
                "destination": _first_present(entry, "destination", "ville", "city", "dest", "nom", "airport"),
                "date": _first_present(entry, "date", "jour", "day", "date_flight"),
                "time": _first_present(entry, "time", "heure", "horaire", "heure_depart", "heure_arrivee"),
                "airline": _first_present(entry, "airline", "compagnie", "carrier", "compagnie_nom"),
                "airline_raw": _first_present(entry, "airline_raw", "compagnie_raw", "compagnie_txt", "airline_name"),
                "flight_number": _first_present(entry, "flight_number", "numero", "nvol", "flight", "vol"),
                "status_raw": _first_present(entry, "status_raw", "status", "etat", "state", "info"),
            }
            if flight["flight_number"] is None and isinstance(entry.get("numeroVol"), str):
                flight["flight_number"] = entry.get("numeroVol")

            if flight["airline"] is None and flight["airline_raw"] is not None:
                flight["airline"] = flight["airline_raw"]

            status_code, status_raw, delayed = normalize_status(flight["status_raw"])
            flight["status_code"] = status_code
            flight["status_raw"] = status_raw
            flight["delayed"] = delayed
            flight["early"] = False

            raw_destination = flight.get("destination")
            if isinstance(raw_destination, str):
                flight["destination"] = raw_destination.strip()
            if isinstance(flight.get("date"), str):
                flight["date"] = flight["date"].strip()
            if isinstance(flight.get("time"), str):
                flight["time"] = flight["time"].strip()
            if isinstance(flight.get("flight_number"), str):
                flight["flight_number"] = flight["flight_number"].strip()
            if flight.get("airline") is not None and isinstance(flight["airline"], str):
                flight["airline"] = flight["airline"].strip()
            if flight.get("airline_raw") is not None and isinstance(flight["airline_raw"], str):
                flight["airline_raw"] = flight["airline_raw"].strip()
            normalized.append(flight)
            continue

        if isinstance(entry, (list, tuple)) and len(entry) >= 4:
            # Cas de tableaux de valeurs simples: [destination, date, time, airline, flight_number, status]
            destination = entry[0] if entry[0] not in (None, "") else None
            date_value = entry[1] if len(entry) > 1 else None
            time_value = entry[2] if len(entry) > 2 else None
            airline_value = entry[3] if len(entry) > 3 else None
            flight_number = entry[4] if len(entry) > 4 else None
            status_value = entry[5] if len(entry) > 5 else None
            normalized.append(
                {
                    "destination": destination,
                    "date": date_value,
                    "time": time_value,
                    "airline": airline_value,
                    "airline_raw": airline_value,
                    "flight_number": flight_number,
                    "status_code": None,
                    "status_raw": None,
                    "delayed": False,
                    "early": False,
                }
            )

    return normalized


async def fetch_html(url: str) -> tuple[str, dict]:
    """Fetch HTML using Playwright to execute JavaScript."""
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        await page.set_extra_http_headers(HEADERS)
        try:
            await page.goto(url, wait_until="networkidle", timeout=30000)
        except Exception as e:
            print(f"ERREUR: Erreur lors de la navigation vers {url}: {e}", file=sys.stderr)
            await browser.close()
            raise

        js_payload = await page.evaluate(
            """
            () => {
                const names = ['tVolsDep', 'volsDep', 'volsArr'];
                const out = {};
                for (const name of names) {
                    try {
                        if (typeof window[name] !== 'undefined') {
                            out[name] = JSON.parse(JSON.stringify(window[name]));
                        }
                    } catch (e) {
                        out[name] = null;
                    }
                }
                return out;
            }
            """
        )

        selector_found = False
        selectors_to_try = [
            "text=Prochains départs",
            "text=Départs",
            "text=Arrivées",
            "text=/.*vol.*",
        ]

        for selector in selectors_to_try:
            try:
                await page.wait_for_selector(selector, timeout=5000)
                print(f"INFO: Sélecteur trouvé: {selector}", file=sys.stderr)
                selector_found = True
                break
            except Exception:
                continue

        if not selector_found:
            print(f"ATTENTION: Aucun des sélecteurs n'a pu être trouvé", file=sys.stderr)

        html = await page.content()

        try:
            DEBUG_PATH.parent.mkdir(parents=True, exist_ok=True)
            DEBUG_PATH.write_text(html, encoding="utf-8")
            print(f"DEBUG: HTML sauvegardé dans {DEBUG_PATH}", file=sys.stderr)
        except Exception as e:
            print(f"ERREUR: Impossible de sauvegarder le debug HTML: {e}", file=sys.stderr)

        await browser.close()
    return html, js_payload


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

        flight_number = None
        flight_num_match = re.match(r"\s*([A-Z0-9]{2,3}\d{2,5}[A-Z]?)\b", flight_line)
        if flight_num_match:
            flight_number = flight_num_match.group(1)

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

    deduped: dict[tuple, dict] = {}
    for f in flights:
        key = f.pop("_dedupe_key")
        if key not in deduped:
            deduped[key] = f
        elif not deduped[key].get("status_raw") and f.get("status_raw"):
            deduped[key] = f

    return list(deduped.values())


def build_payload(html: str, js_payload: dict | None = None) -> dict:
    # Priorité 1 : variables JS globales du site (on a vu tVolsDep / volsDep / volsArr)
    if js_payload:
        departures = normalize_js_flights(js_payload.get("volsDep") or js_payload.get("tVolsDep"), kind="departures")
        arrivals = normalize_js_flights(js_payload.get("volsArr"), kind="arrivals")
        if departures or arrivals:
            return {
                "scraped_at": datetime.now(tz=PARIS_TZ).isoformat(),
                "source_url": SOURCE_URL,
                "departures": departures,
                "arrivals": arrivals,
            }

    lines = extract_lines(html)

    print(f"DEBUG: Nombre total de lignes extraites: {len(lines)}", file=sys.stderr)
    if lines:
        print(f"DEBUG: Premières 20 lignes:", file=sys.stderr)
        for i, line in enumerate(lines[:20]):
            print(f"  {i}: {line}", file=sys.stderr)
        print(f"DEBUG: Dernières 10 lignes:", file=sys.stderr)
        for i, line in enumerate(lines[-10:], start=len(lines)-10):
            print(f"  {i}: {line}", file=sys.stderr)

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

    print(f"DEBUG: Lignes de départs trouvées: {len(departures_lines)}", file=sys.stderr)
    print(f"DEBUG: Lignes d'arrivées trouvées: {len(arrivals_lines)}", file=sys.stderr)

    if not departures_lines and not arrivals_lines:
        print(f"DEBUG: Cherchant marqueurs alternatifs...", file=sys.stderr)
        for i, line in enumerate(lines):
            if 'départ' in line.lower() or 'arrivée' in line.lower() or 'vol' in line.lower():
                print(f"  {i}: {line}", file=sys.stderr)

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


async def main() -> int:
    try:
        html, js_payload = await fetch_html(SOURCE_URL)
    except Exception as exc:
        print(f"ERREUR: impossible de récupérer la page source : {exc}", file=sys.stderr)
        return 1

    payload = build_payload(html, js_payload)

    if not payload["departures"] and not payload["arrivals"]:
        print(
            "ATTENTION: aucun vol détecté — la structure de la page a peut-être "
            "changé, ou son contenu n'a pas pu être chargé. Vérifier l'HTML sauvegardé "
            "dans _debug_page.html",
            file=sys.stderr,
        )

        if OUTPUT_PATH.exists():
            print("INFO: Fichier existant conservé, retour de la dernière donnée valide.", file=sys.stderr)
            return 0

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

    alerts_path = OUTPUT_PATH.parent / "_pending_alerts.json"
    alerts_path.write_text(json.dumps(alerts, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"OK: {len(payload['departures'])} départs, {len(payload['arrivals'])} arrivées écrits dans {OUTPUT_PATH}")
    print(f"Alertes détectées : {len(alerts)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
