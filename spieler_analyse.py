"""
Kickbase Spieler-Analyse
========================

Sammelt einmal pro Lauf die Daten ALLER Bundesliga-Spieler und schreibt sie
kompakt nach spieler_daten.json. Die Webseite (index.html) liest die Datei und
zeigt pro Spieler ein Popup mit:

  - Punkteschnitt, Spielzeit, Punkte pro 90 Min, Preis-Leistung, Form, Konstanz
  - vergangene Spiele: Gegner, Ergebnis, Minuten, Punkte, Gegner-Tabellenplatz
  - die naechsten 3 Gegner: Tabellenplatz, Gegentore, Gegner-Faktor
  - Punkte-Herkunft: Scorerpunkte vs. Grundpunkte (nur Transfermarkt-Spieler)
  - bei Transfermarkt-Spielern: Preis, Ablauf, welche Mitspieler mitbieten koennen

Datenquellen (Kickbase v4, siehe https://kevinskyba.github.io/kickbase-api-doc/):
  GET /v4/competitions/{cid}/table                       Tabelle
  GET /v4/competitions/{cid}/matchdays                   Spielplan + Ergebnisse
  GET /v4/competitions/{cid}/teams/{tid}/teamprofile     Kader je Verein
  GET /v4/competitions/{cid}/players/{pid}               Spieler-Details
  GET /v4/competitions/{cid}/players/{pid}/performance   Punkte je Spieltag
  GET /v4/competitions/{cid}/playercenter/{pid}          Ereignisse eines Spiels
  GET /v4/live/eventtypes                                Namen der Ereignistypen
  GET /v4/leagues/{lid}/market                           Transfermarkt eurer Liga

Nutzung:
    python spieler_analyse.py
"""

from __future__ import annotations

import json
import os
import re
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import requests

import kickbase_tracker as kt

BASE = "https://api.kickbase.com"
COMPETITION_ID = "1"  # 1 = Bundesliga

OUTPUT_FILE = "spieler_daten.json"
# Ereignisse eines Spiels aendern sich nach Spielende nicht mehr -> dauerhaft cachen
HERKUNFT_CACHE_FILE = "punkte_herkunft_cache.json"

MAX_WORKERS = 6          # parallele Requests (hoeher = schneller, aber Rate-Limit-Risiko)
NAECHSTE_SPIELE = 3      # wie viele kommende Gegner analysiert werden
FORM_SPIELE = 3          # Form = Schnitt der letzten N Einsaetze

POSITION_MAP = {1: "TW", 2: "ABW", 3: "MF", 4: "ST"}

_session = requests.Session()


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

def api_get(path: str, headers: dict, params: dict | None = None, versuche: int = 4):
    """GET mit einfachem Retry/Backoff (429 und 5xx)."""
    url = BASE + path
    for versuch in range(versuche):
        try:
            r = _session.get(url, headers=headers, params=params, timeout=30)
            if r.status_code == 429 or r.status_code >= 500:
                raise requests.HTTPError(f"HTTP {r.status_code}")
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as e:
            if versuch == versuche - 1:
                print(f"  ! {path} fehlgeschlagen: {e}")
                return None
            time.sleep(1.5 * (versuch + 1))
    return None


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------

def _minuten(mp) -> int:
    """'87\'' / '90+3\'' / 87 -> Minuten als int (0, wenn nicht gespielt)."""
    if mp is None:
        return 0
    if isinstance(mp, (int, float)):
        return int(mp)
    zahlen = re.findall(r"\d+", str(mp))
    return sum(int(z) for z in zahlen) if zahlen else 0


def _mittel(werte: list[float]) -> float | None:
    return round(sum(werte) / len(werte), 1) if werte else None


def _aktuelle_saison(perf: dict) -> list[dict]:
    """Spieltage der aktuellen Bundesliga-Saison aus /performance."""
    saisons = [s for s in (perf or {}).get("it", []) if s.get("n") == "Bundesliga"] or (perf or {}).get("it", [])
    if not saisons:
        return []
    aktuell = max(saisons, key=lambda s: s.get("ti", ""))
    return aktuell.get("ph", []) or []


def _beendet(e: dict) -> bool:
    """Spiel beendet? mdst == 2 laut API; zur Sicherheit zusaetzlich: Anstoss
    liegt mehr als 3 Stunden zurueck (aeltere Daten haben teils mdst 0)."""
    if e.get("mdst") == 2:
        return True
    try:
        anstoss = datetime.fromisoformat(str(e.get("md")).replace("Z", "+00:00"))
    except ValueError:
        return False
    return (datetime.now(timezone.utc) - anstoss).total_seconds() > 3 * 3600


def _quoten_zu_prozent(bo: dict | None) -> dict | None:
    """Wettquoten (bo: o1/ox/o2) -> Wahrscheinlichkeiten in %. Nur wenn plausible Dezimalquoten."""
    if not bo:
        return None
    try:
        o = [float(bo["o1"]), float(bo["ox"]), float(bo["o2"])]
    except (KeyError, TypeError, ValueError):
        return None
    if not all(1.01 < x < 100 for x in o):
        return None
    inv = [1 / x for x in o]
    s = sum(inv)
    return {"heim": round(100 * inv[0] / s), "x": round(100 * inv[1] / s), "gast": round(100 * inv[2] / s)}


# ---------------------------------------------------------------------------
# Punkte-Herkunft (Scorer vs. Grundpunkte)
# ---------------------------------------------------------------------------

_SCORER_MUSTER = re.compile(
    r"^(goal \(|goal \"|goal behind|penalty scored|assist|goal set up|"
    r"intentional assist|rebound assist|deflected assist|woodwork assist)",
    re.IGNORECASE,
)
_TEAM_MUSTER = re.compile(
    r"^(starting 11|starting 6|played minutes bonus|game won|game lost|team goal|"
    r"goal conceded|clean sheet|subbed on)",
    re.IGNORECASE,
)


def kategorie(titel: str) -> str:
    """'scorer' (Tore/Vorlagen), 'team' (Startelf, Minuten, Sieg, Zu-Null ...) oder 'aktion'."""
    t = (titel or "").strip()
    if _SCORER_MUSTER.match(t):
        return "scorer"
    if _TEAM_MUSTER.match(t):
        return "team"
    return "aktion"


def lade_herkunft_cache() -> dict:
    if os.path.exists(HERKUNFT_CACHE_FILE):
        with open(HERKUNFT_CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def punkte_herkunft_spiel(headers: dict, league_id: str, pid: str, tag: int,
                          eventtypen: dict) -> dict | None:
    """Zerlegt die Punkte eines Spiels in scorer / team / aktion."""
    d = api_get(f"/v4/competitions/{COMPETITION_ID}/playercenter/{pid}", headers,
                params={"dayNumber": tag, "leagueId": league_id})
    if not d:
        return None
    summe = {"scorer": 0, "team": 0, "aktion": 0}
    for ev in d.get("events", []) or []:
        p = ev.get("p") or 0
        if not p:
            continue
        summe[kategorie(eventtypen.get(ev.get("eti"), ""))] += p
    gesamt = d.get("p")
    if gesamt is not None:
        # Rundungs-/Korrektur-Ereignisse der Aktions-Kategorie zuschlagen,
        # damit die Summe exakt den offiziellen Spielpunkten entspricht.
        summe["aktion"] += gesamt - sum(summe.values())
    return summe


# ---------------------------------------------------------------------------
# Datensammlung
# ---------------------------------------------------------------------------

def sammle_rohdaten(headers: dict) -> dict:
    print("Lade Tabelle und Spielplan ...")
    tabelle = api_get(f"/v4/competitions/{COMPETITION_ID}/table", headers) or {}
    spielplan = api_get(f"/v4/competitions/{COMPETITION_ID}/matchdays", headers) or {}

    team_ids = [t["tid"] for t in tabelle.get("it", [])]
    print(f"Lade Kader von {len(team_ids)} Vereinen ...")
    with ThreadPoolExecutor(MAX_WORKERS) as ex:
        profile = list(ex.map(
            lambda tid: api_get(f"/v4/competitions/{COMPETITION_ID}/teams/{tid}/teamprofile", headers),
            team_ids))

    kader = []
    for prof in profile:
        for p in (prof or {}).get("it", []) or []:
            kader.append(p)
    print(f"{len(kader)} Spieler gefunden, lade Details + Leistungsdaten ...")

    def details_und_perf(p):
        pid = str(p["i"])
        det = api_get(f"/v4/competitions/{COMPETITION_ID}/players/{pid}", headers) or {}
        perf = None
        # Spieler ohne einen einzigen Punkt brauchen keinen Leistungsverlauf
        if (det.get("tp") or p.get("ap") or 0) != 0:
            perf = api_get(f"/v4/competitions/{COMPETITION_ID}/players/{pid}/performance", headers)
        return pid, det, perf

    with ThreadPoolExecutor(MAX_WORKERS) as ex:
        ergebnisse = list(ex.map(details_und_perf, kader))

    return {
        "tabelle": tabelle,
        "spielplan": spielplan,
        "kader": kader,
        "details": {pid: det for pid, det, _ in ergebnisse},
        "performance": {pid: perf for pid, _, perf in ergebnisse},
    }


# ---------------------------------------------------------------------------
# Analyse
# ---------------------------------------------------------------------------

def baue_teams(tabelle: dict, spielplan: dict) -> tuple[dict, list[dict]]:
    teams: dict[str, dict] = {}
    for t in tabelle.get("it", []):
        teams[str(t["tid"])] = {
            "n": t.get("tn", "?"), "sy": None, "pl": t.get("cpl"), "pkt": t.get("cp"),
            "sp": t.get("mc") or 0, "td": t.get("gd"), "gt": 0, "ggt": 0, "gesp": 0,
        }

    spiele = []
    for tag in spielplan.get("it", []):
        for m in tag.get("it", []):
            t1, t2 = str(m["t1"]), str(m["t2"])
            for tid, sy in ((t1, m.get("t1sy")), (t2, m.get("t2sy"))):
                if tid in teams and sy:
                    teams[tid]["sy"] = sy
            beendet = m.get("st") == 2
            spiele.append({
                "tag": m.get("day", tag.get("day")), "dt": m.get("dt"), "t1": t1, "t2": t2,
                "g1": m.get("t1g"), "g2": m.get("t2g"), "beendet": beendet,
                "quoten": _quoten_zu_prozent(m.get("bo")),
            })
            if beendet and t1 in teams and t2 in teams:
                teams[t1]["gt"] += m.get("t1g") or 0
                teams[t1]["ggt"] += m.get("t2g") or 0
                teams[t2]["gt"] += m.get("t2g") or 0
                teams[t2]["ggt"] += m.get("t1g") or 0
                teams[t1]["gesp"] += 1
                teams[t2]["gesp"] += 1

    for t in teams.values():
        t["sy"] = t["sy"] or t["n"][:3].upper()
        t["ggt_schnitt"] = round(t["ggt"] / t["gesp"], 2) if t["gesp"] else None
        t["gt_schnitt"] = round(t["gt"] / t["gesp"], 2) if t["gesp"] else None
    return teams, spiele


def spiele_eines_spielers(ph: list[dict]) -> list[dict]:
    """Vergangene, beendete Spiele (inkl. Nicht-Einsaetze) der aktuellen Saison."""
    out = []
    for e in ph:
        if not _beendet(e):
            continue
        if e.get("t1") is None or e.get("t2") is None:
            continue
        team = str(e.get("pt") or "")
        heim = team == str(e["t1"])
        gegner = str(e["t2"] if heim else e["t1"])
        tore_eigen = e.get("t1g") if heim else e.get("t2g")
        tore_gegner = e.get("t2g") if heim else e.get("t1g")
        out.append({
            "tag": e.get("day"),
            "geg": gegner,
            "h": heim,
            "erg": f"{tore_eigen}:{tore_gegner}" if tore_eigen is not None else None,
            "min": _minuten(e.get("mp")),
            "p": e.get("p") or 0,
        })
    out.sort(key=lambda x: x["tag"] or 0)
    return out


def gegner_faktoren(spieler_spiele: dict[str, list[dict]], positionen: dict[str, int]) -> dict:
    """Wie viele Punkte ein Verein pro Position zulaesst, relativ zum Liga-Schnitt.
    1.15 = gegen diesen Gegner holen Spieler dieser Position 15 % mehr als ueblich.
    Mit Schrumpfung Richtung 1.0, solange erst wenige Spiele vorliegen."""
    summen: dict[tuple, list[float]] = {}
    liga: dict[int, list[float]] = {}
    for pid, spiele in spieler_spiele.items():
        pos = positionen.get(pid)
        for s in spiele:
            if s["min"] <= 0:
                continue
            summen.setdefault((s["geg"], pos), []).append(s["p"])
            liga.setdefault(pos, []).append(s["p"])

    liga_schnitt = {pos: sum(v) / len(v) for pos, v in liga.items() if v}
    k = 8  # "virtuelle" Durchschnittsspiele zur Glaettung
    faktoren = {}
    for (geg, pos), werte in summen.items():
        basis = liga_schnitt.get(pos)
        if not basis:
            continue
        geglaettet = (sum(werte) + k * basis) / (len(werte) + k)
        faktoren.setdefault(geg, {})[pos] = round(geglaettet / basis, 2)
    return faktoren


def analysiere(roh: dict, markt: dict, herkunft: dict, state: dict) -> dict:
    teams, alle_spiele = baue_teams(roh["tabelle"], roh["spielplan"])
    jetzt = datetime.now(timezone.utc).isoformat()

    # Spieltage je Spieler
    spieler_spiele = {}
    positionen = {}
    for p in roh["kader"]:
        pid = str(p["i"])
        positionen[pid] = p.get("pos")
        spieler_spiele[pid] = spiele_eines_spielers(_aktuelle_saison(roh["performance"].get(pid)))

    faktoren = gegner_faktoren(spieler_spiele, positionen)

    # Kommende Spiele je Verein
    kommend: dict[str, list[dict]] = {}
    for s in sorted((s for s in alle_spiele if not s["beendet"]), key=lambda s: (s["dt"] or "", s["tag"] or 0)):
        for tid, gegner, heim in ((s["t1"], s["t2"], True), (s["t2"], s["t1"], False)):
            liste = kommend.setdefault(tid, [])
            if len(liste) < NAECHSTE_SPIELE:
                q = s["quoten"]
                sieg = (q["heim"] if heim else q["gast"]) if q else None
                liste.append({"tag": s["tag"], "dt": s["dt"], "geg": gegner, "h": heim, "sieg": sieg})

    # Preis-Leistung je Position fuer Perzentil-Rang
    pl_werte: dict[int, list[float]] = {}

    spieler_out = []
    for p in roh["kader"]:
        pid = str(p["i"])
        det = roh["details"].get(pid) or {}
        tid = str(det.get("tid") or p.get("tid") or "")
        pos = p.get("pos") or det.get("pos")
        mv = det.get("mv") or p.get("mv") or 0
        spiele = spieler_spiele[pid]
        einsaetze = [s for s in spiele if s["min"] > 0]
        punkte = [s["p"] for s in einsaetze]
        minuten = sum(s["min"] for s in einsaetze)
        tp = det.get("tp")
        if tp is None:
            tp = sum(punkte)
        ap = det.get("ap") if det.get("ap") is not None else (p.get("ap") or _mittel(punkte) or 0)
        team_spiele = teams.get(tid, {}).get("gesp") or len(spiele) or 0

        pl = round(ap / (mv / 1e6), 1) if mv and ap else None
        if pl is not None and einsaetze:
            pl_werte.setdefault(POSITION_MAP.get(pos, "?"), []).append(pl)

        oben = [s["p"] for s in einsaetze if (teams.get(s["geg"], {}).get("pl") or 99) <= 9]
        unten = [s["p"] for s in einsaetze if (teams.get(s["geg"], {}).get("pl") or 0) >= 10]

        naechste = []
        for n in kommend.get(tid, []):
            naechste.append({**n, "fk": faktoren.get(n["geg"], {}).get(pos)})
        plaetze = [teams.get(n["geg"], {}).get("pl") for n in naechste if teams.get(n["geg"], {}).get("pl")]
        faktor_liste = [n["fk"] for n in naechste if n["fk"]]

        eintrag = {
            "id": pid,
            "n": det.get("ln") or p.get("n") or "?",
            "vn": det.get("fn") or "",
            "tid": tid,
            "pos": POSITION_MAP.get(pos, "?"),
            "mv": mv,
            "mvd": det.get("tfhmvt") if det.get("tfhmvt") is not None else p.get("sdmvt"),
            "st": det.get("st") if det.get("st") is not None else p.get("st"),
            "stxt": det.get("stxt") or "",
            "tp": tp,
            "ap": ap,
            "g": det.get("g") or 0,
            "a": det.get("a") or 0,
            "einsaetze": len(einsaetze),
            "team_spiele": team_spiele,
            "min": minuten,
            "min_schnitt": round(minuten / len(einsaetze)) if einsaetze else 0,
            "startquote": round(100 * sum(1 for s in einsaetze if s["min"] >= 60) / team_spiele) if team_spiele else None,
            "p90": round(sum(punkte) / minuten * 90) if minuten >= 45 else None,
            "pl": pl,
            "form": _mittel(punkte[-FORM_SPIELE:]),
            "streuung": round(statistics.pstdev(punkte)) if len(punkte) >= 2 else None,
            "min_p": min(punkte) if punkte else None,
            "max_p": max(punkte) if punkte else None,
            "ueber100": round(100 * sum(1 for x in punkte if x >= 100) / len(punkte)) if punkte else None,
            "vs_oben": _mittel(oben),
            "vs_unten": _mittel(unten),
            "spiele": spiele,
            "naechste": naechste,
            "naechste_pl": _mittel(plaetze),
            "naechste_fk": round(sum(faktor_liste) / len(faktor_liste), 2) if faktor_liste else None,
        }

        h = herkunft.get(pid)
        if h:
            eintrag["herkunft"] = h
        spieler_out.append(eintrag)

    # Preis-Leistungs-Rang innerhalb der Position (0-100, hoeher = besser)
    for s in spieler_out:
        werte = pl_werte.get(s["pos"], [])
        if s["pl"] is not None and werte and s["einsaetze"]:
            unter = sum(1 for w in werte if w < s["pl"])
            s["pl_rang"] = round(100 * unter / max(len(werte) - 1, 1))

    # Transfermarkt + wer mitbieten kann
    manager = []
    for mid, m in (state or {}).items():
        if m.get("history"):
            manager.append({"id": mid, "n": (m.get("name") or "").strip(),
                            "max": m["history"][-1].get("max_gebot"),
                            "ich": mid == getattr(kt, "EIGENE_MANAGER_ID", None)})
    manager.sort(key=lambda x: -(x["max"] or 0))

    markt_out = []
    for it in (markt or {}).get("it", []) or []:
        pid = str(it.get("i") or it.get("pi") or "")
        if not pid:
            continue
        verkaeufer = it.get("u") or {}
        markt_out.append({
            "id": pid,
            "preis": it.get("prc") or it.get("mv"),
            "ablauf_s": it.get("exs"),
            "verkaeufer": (verkaeufer.get("n") if isinstance(verkaeufer, dict) else None) or "Kickbase",
            "gebote": it.get("ofc"),
        })

    return {
        "stand": jetzt,
        "spieltag": roh["spielplan"].get("day"),
        "teams": {tid: {k: t[k] for k in ("n", "sy", "pl", "pkt", "td", "gt_schnitt", "ggt_schnitt")}
                  for tid, t in teams.items()},
        "manager": manager,
        "markt": markt_out,
        "spieler": spieler_out,
    }


def berechne_herkunft(headers: dict, league_id: str, roh: dict, spieler_ids: list[str]) -> dict:
    """Punkte-Herkunft fuer die angegebenen Spieler (alle Einsaetze der Saison).
    Bereits beendete Spiele kommen aus dem Cache und werden nicht neu geladen."""
    if not spieler_ids:
        return {}
    eventtypen_roh = api_get("/v4/live/eventtypes", headers) or {}
    eventtypen = {e["i"]: e.get("ti", "") for e in eventtypen_roh.get("it", [])}
    if not eventtypen:
        print("  ! Ereignistypen nicht abrufbar - Punkte-Herkunft wird uebersprungen.")
        return {}

    cache = lade_herkunft_cache()
    aufgaben = []
    for pid in spieler_ids:
        spiele = spiele_eines_spielers(_aktuelle_saison(roh["performance"].get(pid)))
        for s in spiele:
            if s["min"] > 0 and f"{pid}|{s['tag']}" not in cache:
                aufgaben.append((pid, s["tag"]))

    if aufgaben:
        print(f"Lade Punkte-Herkunft fuer {len(aufgaben)} Spiel(e) ...")
    with ThreadPoolExecutor(MAX_WORKERS) as ex:
        for (pid, tag), erg in zip(aufgaben, ex.map(
                lambda a: punkte_herkunft_spiel(headers, league_id, a[0], a[1], eventtypen), aufgaben)):
            if erg:
                cache[f"{pid}|{tag}"] = erg

    with open(HERKUNFT_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, separators=(",", ":"))

    ergebnis = {}
    for pid in spieler_ids:
        spiele = spiele_eines_spielers(_aktuelle_saison(roh["performance"].get(pid)))
        pro_spiel = []
        for s in spiele:
            h = cache.get(f"{pid}|{s['tag']}")
            if s["min"] > 0 and h:
                pro_spiel.append({"tag": s["tag"], **h})
        if pro_spiel:
            ergebnis[pid] = {
                "scorer": sum(x["scorer"] for x in pro_spiel),
                "team": sum(x["team"] for x in pro_spiel),
                "aktion": sum(x["aktion"] for x in pro_spiel),
                "spiele": pro_spiel,
            }
    return ergebnis


# ---------------------------------------------------------------------------
# Hauptablauf
# ---------------------------------------------------------------------------

def main() -> None:
    start = time.time()
    token = kt.login(kt.KICKBASE_EMAIL, kt.KICKBASE_PASSWORD)
    headers = kt.auth_headers(token)
    league_id = kt.get_league_id(headers)

    roh = sammle_rohdaten(headers)
    markt = api_get(f"/v4/leagues/{league_id}/market", headers) or {}
    markt_ids = [str(it.get("i") or it.get("pi")) for it in markt.get("it", []) or [] if it.get("i") or it.get("pi")]
    print(f"{len(markt_ids)} Spieler auf dem Transfermarkt.")

    herkunft = berechne_herkunft(headers, league_id, roh, markt_ids)
    daten = analysiere(roh, markt, herkunft, kt.load_state())

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(daten, f, ensure_ascii=False, separators=(",", ":"))
    groesse = os.path.getsize(OUTPUT_FILE) / 1024
    print(f"{OUTPUT_FILE} geschrieben: {len(daten['spieler'])} Spieler, "
          f"{len(daten['markt'])} auf dem Markt, {groesse:,.0f} KB, {time.time() - start:.0f} s.")


if __name__ == "__main__":
    main()
