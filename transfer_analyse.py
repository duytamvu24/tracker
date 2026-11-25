"""
Kickbase Transfer-Analyse
=========================

Wertet das Kauf- und Verkaufsverhalten jedes Managers seit dem letzten Reset aus
und schreibt das Ergebnis nach transfer_analyse.json (Reiter "Transfers" im
Liga-Cockpit).

Pro Kauf:
  - Wochentag, Kaufpreis, Marktwert zum Kaufzeitpunkt, OVP (Überzahlung)
  - Marktwert-Anstieg der letzten Nacht vor dem Kauf (in Euro)
  - Naechte (= Marktwert-Updates) bis zum naechsten Anpfiff und daraus der
    theoretische Gewinn bis Anpfiff = Anstieg x Naechte, wenn man ohne OVP kauft
  - OVP in Euro und als Anteil am theoretischen Gewinn
  - bei Verkauf: Verkaufspreis, Marktwert-Anstieg bis zum Verkauf, TF-Gewinn,
    ob die OVP durch den Anstieg wieder eingespielt wurde
  - Typ: "trade"    = vor dem nächsten Anpfiff wieder verkauft
         "gehalten" = mindestens einen Spieltag im Kader, später verkauft
         "offen"    = noch im Kader (unrealisierter Gewinn = MW jetzt - Kaufpreis)
         "reset"    = beim Winter-Reset noch im Kader (Kader neu ausgelost, kein Verkauf)

Ausgewertet wird die ganze Saison ueber Winter-Resets hinweg, je Manager zusaetzlich
getrennt nach Hinrunde/Rueckrunde (Abschnitte aus state.json "perioden").

Dazu je Manager: Zusammenfassung, Auswertung nach Wochentag und OVP-Stufe und
ein Bieter-Profil (welche OVP zahlt er bei welchem Marktwert-Trend) für den
Gebots-Rechner.

Datenquellen:
  transfers_store.json / state.json / spieltage.json (vom Tracker)
  spieler_daten.json (von spieler_analyse.py; Namen, Positionen, Punkte)
  GET /v4/leagues/{lid}/managers/{mid}/transfer?start=N        Transfers (mit Spieler-ID)
  GET /v4/competitions/1/players/{pid}/marketvalue/365          Marktwert je Tag

Nutzung:
    python transfer_analyse.py
"""

from __future__ import annotations

import json
import os
import statistics
import sys
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import date

import pandas as pd
import requests

import kickbase_tracker as kt

BASE = "https://api.kickbase.com"
OUTPUT_FILE = "transfer_analyse.json"
MW_CACHE_FILE = "mw_verlauf.json"
MW_CACHE_STUNDEN = 20        # Marktwert-Verlauf eines Spielers hoechstens so oft neu laden
MW_DRINGEND_STUNDEN = 3      # ... ausser er wurde in den letzten 3 Tagen gehandelt
MAX_WORKERS = 3              # Kickbase drosselt bei zu vielen Abrufen (HTTP 429)
MAX_FEHLEND = 0.05           # fehlen mehr Marktwerte, bleibt die alte Analyse stehen
TZ = "Europe/Berlin"
WOCHENTAGE = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
EPOCHE = date(1970, 1, 1)

# Stufen fuer Auswertungen
# OVP als Anteil am theoretischen Gewinn bis Anpfiff (= Anstieg letzte Nacht x Naechte bis Anpfiff)
ANTEIL_STUFEN = [(-1e9, 0.0, "unter MW"), (0.0, 0.25, "0–25 %"), (0.25, 0.5, "25–50 %"),
                 (0.5, 0.75, "50–75 %"), (0.75, 1.0, "75–100 %"), (1.0, 1e9, "über 100 %")]
# Marktwert-Anstieg der letzten Nacht vor dem Kauf, in Euro
ANSTIEG_STUFEN = [(-1e12, 1.0, "fallend/0"), (1.0, 100_000, "bis 100k"), (100_000, 200_000, "100–200k"),
                  (200_000, 300_000, "200–300k"), (300_000, 500_000, "300–500k"),
                  (500_000, 1_000_000, "500k–1 Mio"), (1_000_000, 1e12, "über 1 Mio")]

_session = requests.Session()


def norm(s: str) -> str:
    return unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower().strip()


def berlin(ts) -> pd.Timestamp:
    return kt._berlin(ts)


def stufe(wert, stufen):
    if wert is None:
        return None
    for lo, hi, name in stufen:
        if lo <= wert < hi:
            return name
    return stufen[-1][2]


def quantil(werte: list[float], q: float):
    werte = sorted(w for w in werte if w is not None)
    if not werte:
        return None
    if len(werte) == 1:
        return werte[0]
    pos = (len(werte) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(werte) - 1)
    return werte[lo] + (werte[hi] - werte[lo]) * (pos - lo)


def runde(x, n=4):
    return None if x is None else round(x, n)


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

def api_get(pfad: str, headers: dict, versuche: int = 6):
    for i in range(versuche):
        try:
            r = _session.get(BASE + pfad, headers=headers, timeout=30)
            if r.status_code == 404:
                return None
            if r.status_code == 429 and i < versuche - 1:
                warte = r.headers.get("Retry-After")
                time.sleep(float(warte) if warte and warte.replace(".", "").isdigit() else min(60, 2 ** (i + 1)))
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001
            if i == versuche - 1:
                print(f"Hinweis: {pfad} nicht abrufbar: {e}")
                return None
    return None


def transfers_nachladen(league_id: str, headers: dict, store: dict, schwelle: pd.Timestamp) -> None:
    """Blaettert den Transfer-Feed jedes Managers zurueck (start=0, 24, ...), bis
    der Reset erreicht ist. Ergaenzt so Spieler-ID und Handelspartner auch fuer
    aeltere, schon archivierte Transfers."""
    for manager_id in list(store.keys()):
        start, seiten = 0, 0
        while seiten < 60:
            d = api_get(f"/v4/leagues/{league_id}/managers/{manager_id}/transfer?start={start}", headers)
            it = (d or {}).get("it", []) or []
            if not it:
                break
            ms = store.get(manager_id, {})
            schon_komplett = all(kt._transfer_key(t) in ms and "pi" in ms[kt._transfer_key(t)] for t in it)
            kt.archiviere_transfers(store, manager_id, it)
            if schon_komplett and seiten > 0:
                break                          # aeltere Seiten wurden schon frueher ergaenzt
            seiten += 1
            start += len(it)
            if min(berlin(t["dt"]) for t in it) <= schwelle:
                break


def lade_mw_verlauf(pids: set[str], headers: dict, dringend: set[str] | None = None) -> dict:
    """Marktwert je Tag (Tag = Tage seit 1970) fuer alle Spieler, mit Cache."""
    cache = {}
    if os.path.exists(MW_CACHE_FILE):
        try:
            with open(MW_CACHE_FILE, "r", encoding="utf-8") as f:
                cache = json.load(f)
        except (ValueError, OSError):
            cache = {}
    jetzt = pd.Timestamp.now(tz=TZ)
    dringend = dringend or set()

    def alter(pid):
        return jetzt - berlin(cache[pid].get("stand", "2000-01-01T00:00:00+00:00"))

    faellig = [pid for pid in pids
               if pid not in cache or alter(pid) > pd.Timedelta(hours=MW_CACHE_STUNDEN)
               or (pid in dringend and alter(pid) > pd.Timedelta(hours=MW_DRINGEND_STUNDEN))]
    print(f"Marktwert-Verlauf: {len(pids)} Spieler, {len(faellig)} neu zu laden ...")

    def holen(pid):
        d = api_get(f"/v4/competitions/{kt.COMPETITION_ID}/players/{pid}/marketvalue/365", headers)
        return pid, d

    with ThreadPoolExecutor(MAX_WORKERS) as ex:
        for pid, d in ex.map(holen, faellig):
            if not d:
                continue
            alt = cache.get(pid, {}).get("mw", {})
            alt.update({str(e["dt"]): e["mv"] for e in d.get("it", []) or [] if e.get("dt") is not None})
            cache[pid] = {"stand": jetzt.isoformat(), "mw": alt}
    with open(MW_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, separators=(",", ":"))
    return {pid: {int(k): v for k, v in e.get("mw", {}).items()} for pid, e in cache.items()}


# ---------------------------------------------------------------------------
# Marktwert zu einem Zeitpunkt
# ---------------------------------------------------------------------------

def mw_tag(ts: pd.Timestamp, versatz: int) -> int:
    """Index des Marktwert-Eintrags, der zum Zeitpunkt ts gilt.
    Marktwerte aendern sich taeglich gegen 22 Uhr (gleiche Grenze wie der
    Kickbase-Tag des Trackers). versatz wird anhand der Sofortverkaeufe
    kalibriert (die laufen exakt zum Marktwert)."""
    kb = kt.kickbase_day(berlin(ts))          # Datum, an dem der gueltige MW gesetzt wurde
    return (kb - EPOCHE).days + versatz


def mw_bei(mw: dict, pid: str | None, ts, versatz: int):
    if not pid or pid not in mw or ts is None:
        return None
    reihe = mw[pid]
    d = mw_tag(ts, versatz)
    for k in (d, d - 1, d - 2):               # Luecken (z.B. Spieler kurz nicht gelistet) ueberbruecken
        if k in reihe:
            return reihe[k]
    return None


def kalibriere_versatz(sofortverkaeufe: list[tuple[str, pd.Timestamp, int]], mw: dict) -> tuple[int, dict]:
    ergebnis = {}
    for v in (-1, 0, 1):
        treffer = sum(1 for pid, ts, preis in sofortverkaeufe
                      if (m := mw_bei(mw, pid, ts, v)) is not None and abs(m - preis) <= 1)
        ergebnis[v] = treffer
    bester = max(ergebnis, key=lambda v: (ergebnis[v], v == 0))
    return bester, ergebnis


# ---------------------------------------------------------------------------
# Auswertung
# ---------------------------------------------------------------------------

def spieler_ids(store: dict, spieler: list[dict]) -> tuple[dict, dict]:
    """Name -> Spieler-ID (aus dem Feed selbst, sonst aus spieler_daten.json)."""
    aus_feed = {}
    for ms in store.values():
        for t in ms.values():
            if t.get("pi"):
                aus_feed.setdefault(norm(t["pn"]), set()).add(str(t["pi"]))
    aus_daten = {}
    for p in spieler:
        for k in {norm(p.get("n", "")), norm(f'{p.get("vn", "")} {p.get("n", "")}')}:
            aus_daten.setdefault(k, set()).add(str(p["id"]))
    return aus_feed, aus_daten


def pid_fuer(t: dict, aus_feed: dict, aus_daten: dict, mw: dict, versatz: int) -> str | None:
    if t.get("pi"):
        return str(t["pi"])
    kand = aus_feed.get(norm(t["pn"])) or aus_daten.get(norm(t["pn"])) \
        or aus_daten.get(norm(t["pn"]).split(" ")[-1]) or set()
    if len(kand) == 1:
        return next(iter(kand))
    best = None
    for pid in kand:                           # mehrdeutiger Name: MW am naechsten am Preis
        m = mw_bei(mw, pid, berlin(t["dt"]), versatz)
        if m and (best is None or abs(t["trp"] - m) < best[0]):
            best = (abs(t["trp"] - m), pid)
    return best[1] if best else None


def lade_perioden(state: dict, manager_ids: list[str]) -> list[tuple[str, pd.Timestamp]]:
    """Abschnitte der laufenden Saison aus state.json: [("Hinrunde", start), ("Rückrunde", start), ...].
    Der Tracker legt beim Reset je Manager "perioden" an ({"start", "art": "saison"|"winter"});
    die Saison beginnt mit dem letzten "saison"-Eintrag."""
    eintraege = []
    for mid in manager_ids:
        per = state[mid].get("perioden") or (
            [{"start": state[mid]["reset_threshold"], "art": "saison"}] if state[mid].get("reset_threshold") else [])
        if len(per) > len(eintraege):
            eintraege = per
    eintraege = [e for e in eintraege if e.get("start")]
    if not eintraege:
        return [("Saison", pd.Timestamp("2000-01-01", tz=TZ))]
    letzte_saison = max((i for i, e in enumerate(eintraege) if e.get("art") == "saison"), default=0)
    abschnitte = eintraege[letzte_saison:]
    namen = ["Hinrunde", "Rückrunde"]
    return [(namen[i] if i < len(namen) else f"Abschnitt {i + 1}", berlin(e["start"])) for i, e in enumerate(abschnitte)]


def naechte_bis(ts: pd.Timestamp, anpfiff: pd.Timestamp | None) -> int | None:
    """Wie viele naechtliche Marktwert-Updates (~22 Uhr) liegen zwischen Kauf und Anpfiff?"""
    if anpfiff is None:
        return None
    grenze = ts.normalize() + pd.Timedelta(hours=22, minutes=4)
    if grenze <= ts:
        grenze += pd.Timedelta(days=1)
    n = 0
    while grenze < anpfiff:
        n += 1
        grenze += pd.Timedelta(days=1)
    return n


def naechster_anpfiff(ts: pd.Timestamp, anpfiffe: list[tuple[int, pd.Timestamp]]):
    for tag, a in anpfiffe:
        if a > ts:
            return tag, a
    return None, None


def analysiere() -> dict:
    print("Login bei Kickbase ...")
    headers = kt.auth_headers(kt.login(kt.KICKBASE_EMAIL, kt.KICKBASE_PASSWORD))
    league_id = kt.get_league_id(headers)

    state = kt.load_state()
    store = kt.load_transfers_store()
    spieltage = kt.load_spieltage()
    spieler_daten = {}
    if os.path.exists("spieler_daten.json"):
        with open("spieler_daten.json", "r", encoding="utf-8") as f:
            spieler_daten = json.load(f)
    spieler = spieler_daten.get("spieler", []) or []
    sp_by_id = {str(p["id"]): p for p in spieler}
    punkte_je_tag = {str(p["id"]): {g["tag"]: (g.get("p") or 0) for g in p.get("spiele", []) or []} for p in spieler}

    manager_ids = [mid for mid in state if isinstance(state[mid], dict) and state[mid].get("history")]
    perioden = lade_perioden(state, manager_ids)
    saison_start = perioden[0][1]
    resets = [start for _, start in perioden[1:]]
    print("Abschnitte: " + ", ".join(f"{n} ab {s:%d.%m.%Y %H:%M}" for n, s in perioden))

    def periode_von(ts):
        name = perioden[0][0]
        for n, start in perioden:
            if ts > start:
                name = n
        return name

    transfers_nachladen(league_id, headers, store, saison_start)
    kt.save_transfers_store(store)

    anpfiffe = sorted(((int(d), berlin(v["anpfiff"])) for d, v in spieltage.items() if v.get("anpfiff")),
                      key=lambda x: x[1])
    jetzt = pd.Timestamp.now(tz=TZ)

    # alle Transfers der laufenden Saison (ueber Winter-Resets hinweg), mit Zeitstempel
    alle = []
    for mid in manager_ids:
        for t in store.get(mid, {}).values():
            if t.get("excluded"):
                continue
            ts = berlin(t["dt"])
            if ts <= saison_start:
                continue
            alle.append((mid, ts, t))
    alle.sort(key=lambda x: x[1])

    # Gegenpartei: Verkauf von A und Kauf von B zur selben Zeit/zum selben Preis = Deal unter Managern
    index = {}
    for mid, ts, t in alle:
        index.setdefault((t["trp"], int(ts.timestamp())), []).append((mid, t))

    def gegenpartei(mid, ts, t):
        if t.get("othnm"):
            return "manager", t["othnm"]
        for dsek in (0, -1, 1, -2, 2):
            for omid, ot in index.get((t["trp"], int(ts.timestamp()) + dsek), []):
                if omid != mid and ot["tty"] != t["tty"]:
                    return "manager", state[omid]["name"].strip()
        return "kickbase", None

    # Spieler-IDs + Marktwerte
    aus_feed, aus_daten = spieler_ids(store, spieler)
    pids = set()
    for mid, ts, t in alle:
        if t.get("pi"):
            pids.add(str(t["pi"]))
        else:
            pids |= aus_feed.get(norm(t["pn"]), set()) | aus_daten.get(norm(t["pn"]), set()) \
                | aus_daten.get(norm(t["pn"]).split(" ")[-1], set())
    grenze = jetzt - pd.Timedelta(days=3)
    dringend = {str(t["pi"]) for _, ts, t in alle if t.get("pi") and ts >= grenze}
    mw = lade_mw_verlauf(pids, headers, dringend)

    # Kalibrierung mit Sofortverkaeufen an Kickbase (Preis = Marktwert)
    proben = []
    for mid, ts, t in alle:
        if t["tty"] != 1 and gegenpartei(mid, ts, t)[0] == "kickbase":
            pid = pid_fuer(t, aus_feed, aus_daten, mw, 0)
            if pid:
                proben.append((pid, ts, t["trp"]))
    versatz, kal = kalibriere_versatz(proben, mw)
    print(f"Marktwert-Kalibrierung: Versatz {versatz} (Treffer je Versatz: {kal}, Proben: {len(proben)})")

    # Positionen je Manager bilden (FIFO je Spieler)
    positionen = {mid: [] for mid in manager_ids}
    offen = {mid: {} for mid in manager_ids}
    startkader_verkauf = {mid: [] for mid in manager_ids}
    offene_resets = list(resets)

    def reset_schliessen(zeit):
        """Beim Reset werden alle Kader neu ausgelost: offene Kaeufe enden ohne Verkauf."""
        for m_id in manager_ids:
            for stapel in offen[m_id].values():
                for pos in stapel:
                    pos["reset"] = zeit
                    positionen[m_id].append(pos)
            offen[m_id] = {}

    for mid, ts, t in alle:
        while offene_resets and ts > offene_resets[0]:
            reset_schliessen(offene_resets.pop(0))
        pid = pid_fuer(t, aus_feed, aus_daten, mw, versatz) or ("name:" + norm(t["pn"]))
        art, partner = gegenpartei(mid, ts, t)
        if t["tty"] == 1:
            offen[mid].setdefault(pid, []).append({"pid": pid, "name": t["pn"], "kauf": ts, "preis": t["trp"],
                                                   "von": art, "von_name": partner})
        else:
            stapel = offen[mid].get(pid) or []
            if stapel:
                pos = stapel.pop(0)
                pos.update({"verkauf": ts, "vk_preis": t["trp"], "an": art, "an_name": partner})
                positionen[mid].append(pos)
            else:
                startkader_verkauf[mid].append({"pid": pid, "name": t["pn"], "verkauf": ts, "vk_preis": t["trp"],
                                                "an": art, "an_name": partner})
    for zeit in offene_resets:
        if zeit <= jetzt:
            reset_schliessen(zeit)
    for mid in manager_ids:
        for stapel in offen[mid].values():
            positionen[mid].extend(stapel)

    def detail(pos: dict) -> dict:
        pid = pos["pid"]
        kauf = pos["kauf"]
        mw_k = mw_bei(mw, pid, kauf, versatz)
        mw_vortag = mw_bei(mw, pid, kauf - pd.Timedelta(days=1), versatz)
        trend = (mw_k / mw_vortag - 1) if mw_k and mw_vortag else None
        anstieg_vortag = (mw_k - mw_vortag) if mw_k and mw_vortag else None
        tag, anpfiff = naechster_anpfiff(kauf, anpfiffe)
        naechte = naechte_bis(kauf, anpfiff)
        theo = (max(anstieg_vortag, 0) * naechte) if (anstieg_vortag is not None and naechte is not None) else None
        vk = pos.get("verkauf")
        reset = pos.get("reset")
        if reset is not None:
            typ = "reset"
        elif vk is None:
            typ = "offen"
        elif anpfiff is not None and vk < anpfiff:
            typ = "trade"
        else:
            typ = "gehalten"
        ende = vk if vk is not None else (reset if reset is not None else jetzt)
        mw_ende = mw_bei(mw, pid, ende, versatz)
        if typ == "offen" and (sp_by_id.get(pid) or {}).get("mv"):
            mw_ende = sp_by_id[pid]["mv"]          # noch im Kader: aktueller MW aus spieler_daten.json
        ovp_eur = (pos["preis"] - mw_k) if mw_k else None
        anstieg = (mw_ende - mw_k) if (mw_ende and mw_k) else None
        gewinn = (pos["vk_preis"] - pos["preis"]) if vk is not None else None
        spieltage = [t for t, a in anpfiffe if kauf < a < ende and a <= jetzt]
        pk = punkte_je_tag.get(pid, {})
        sp = sp_by_id.get(pid, {})
        return {
            "pid": pid, "name": pos["name"], "pos": sp.get("pos"), "tid": sp.get("tid"),
            "typ": typ, "periode": periode_von(kauf),
            "reset": reset.isoformat() if reset is not None else None,
            "kauf": kauf.isoformat(), "wochentag": WOCHENTAGE[kauf.weekday()], "stunde": kauf.hour,
            "preis": pos["preis"], "von": pos["von"], "von_name": pos["von_name"],
            "mw_kauf": mw_k, "ovp_eur": ovp_eur, "ovp": runde(ovp_eur / mw_k) if ovp_eur is not None and mw_k else None,
            "trend_vortag": runde(trend),
            "anstieg_vortag": anstieg_vortag, "naechte": naechte, "theo_gewinn": theo,
            "tage_bis_anpfiff": runde((anpfiff - kauf).total_seconds() / 86400, 2) if anpfiff is not None else None,
            "verkauf": vk.isoformat() if vk is not None else None,
            "vk_preis": pos.get("vk_preis"), "an": pos.get("an"), "an_name": pos.get("an_name"),
            "mw_ende": mw_ende, "mw_anstieg_eur": anstieg,
            "mw_anstieg": runde(anstieg / mw_k) if anstieg is not None and mw_k else None,
            "haltedauer_tage": runde((ende - kauf).total_seconds() / 86400, 2),
            "tf_gewinn": gewinn,
            "unrealisiert": (mw_ende - pos["preis"]) if (typ == "offen" and mw_ende) else None,
            "ovp_eingespielt": (anstieg >= ovp_eur) if (anstieg is not None and ovp_eur is not None) else None,
            "ovp_anteil": runde(ovp_eur / theo, 3) if (ovp_eur is not None and theo) else None,
            "spieltage": len(spieltage),
            "punkte": sum(pk.get(t, 0) for t in spieltage),
        }

    def zusammenfassung(liste: list[dict]) -> dict:
        def mittel(feld, quelle=None):
            w = [x[feld] for x in (quelle if quelle is not None else liste) if x.get(feld) is not None]
            return runde(statistics.mean(w)) if w else None

        def median(feld, quelle=None):
            w = [x[feld] for x in (quelle if quelle is not None else liste) if x.get(feld) is not None]
            return runde(statistics.median(w)) if w else None

        mit_gewinn = [x for x in liste if x.get("tf_gewinn") is not None]
        return {
            "anzahl": len(liste),
            "gewinn_summe": sum(x["tf_gewinn"] for x in mit_gewinn),
            "gewinner": sum(1 for x in mit_gewinn if x["tf_gewinn"] > 0),
            "verlierer": sum(1 for x in mit_gewinn if x["tf_gewinn"] < 0),
            "gewinn_schnitt": runde(statistics.mean([x["tf_gewinn"] for x in mit_gewinn]), 0) if mit_gewinn else None,
            "ovp_median": median("ovp"), "ovp_schnitt": mittel("ovp"),
            "ovp_eur_median": median("ovp_eur"), "anstieg_vortag_median": median("anstieg_vortag"),
            "theo_median": median("theo_gewinn"), "anteil_median": median("ovp_anteil"),
            "naechte_median": median("naechte"),
            "ovp_eur_summe": sum(x["ovp_eur"] for x in liste if x.get("ovp_eur") is not None),
            "anstieg_median": median("mw_anstieg"),
            "haltedauer_median": median("haltedauer_tage"),
            "tage_bis_anpfiff_median": median("tage_bis_anpfiff"),
            "eingespielt_quote": runde(sum(1 for x in liste if x.get("ovp_eingespielt")) /
                                       max(1, sum(1 for x in liste if x.get("ovp_eingespielt") is not None)), 3),
        }

    def gruppiert(liste, schluessel, reihenfolge):
        aus = []
        for name in reihenfolge:
            g = [x for x in liste if schluessel(x) == name]
            if g:
                aus.append({"gruppe": name, **zusammenfassung(g)})
        return aus

    def bereich(details: list[dict], sk: list[dict]) -> dict:
        """Kennzahlen fuer einen Zeitraum (Saison, Hinrunde, Rueckrunde ...)."""
        trades = [x for x in details if x["typ"] == "trade"]
        gehalten = [x for x in details if x["typ"] == "gehalten"]
        offene = [x for x in details if x["typ"] == "offen"]
        verkauft = trades + gehalten
        return {
            "kaeufe": len(details), "verkaeufe": len(verkauft) + len(sk),
            "kaeufe_von_manager": sum(1 for x in details if x["von"] == "manager"),
            "verkaeufe_an_manager": sum(1 for x in verkauft if x.get("an") == "manager")
                                    + sum(1 for s_ in sk if s_["an"] == "manager"),
            "tf_gewinn": sum(x["tf_gewinn"] for x in verkauft),
            "startkader_anzahl": len(sk),
            "startkader_erloes": sum(s_["vk_preis"] for s_ in sk),
            "unrealisiert": sum(x["unrealisiert"] or 0 for x in offene),
            "durch_reset": sum(1 for x in details if x["typ"] == "reset"),
            "trades": zusammenfassung(trades),
            "gehalten": zusammenfassung(gehalten),
            "offen": zusammenfassung(offene),
            "nach_wochentag": gruppiert(trades, lambda x: x["wochentag"], WOCHENTAGE),
            "nach_ovp": gruppiert(trades, lambda x: ("MW fiel/stagnierte" if not x["theo_gewinn"] else
                                                     stufe(x["ovp_anteil"], ANTEIL_STUFEN)),
                                  [s_[2] for s_ in ANTEIL_STUFEN] + ["MW fiel/stagnierte"]),
            "nach_anstieg": gruppiert(trades, lambda x: stufe(x["anstieg_vortag"], ANSTIEG_STUFEN),
                                      [s_[2] for s_ in ANSTIEG_STUFEN]),
        }

    ergebnis_manager = []
    bieter_alle = []
    for mid in manager_ids:
        details = sorted((detail(p) for p in positionen[mid]), key=lambda x: x["kauf"])
        sk = [{**s_, "verkauf": s_["verkauf"].isoformat(), "wochentag": WOCHENTAGE[s_["verkauf"].weekday()],
               "periode": periode_von(s_["verkauf"])} for s_ in startkader_verkauf[mid]]
        for x in details:
            if x["von"] == "kickbase" and x.get("ovp") is not None:
                bieter_alle.append({"mid": mid, "ovp": x["ovp"], "ovp_eur": x["ovp_eur"], "anstieg": x["anstieg_vortag"],
                                    "naechte": x["naechte"], "anteil": x["ovp_anteil"], "typ": x["typ"],
                                    "gewinn": x["tf_gewinn"], "wt": x["wochentag"], "periode": x["periode"]})
        bereiche = {"Saison": bereich(details, sk)}
        if len(perioden) > 1:
            for n, _ in perioden:
                bereiche[n] = bereich([x for x in details if x["periode"] == n], [s_ for s_ in sk if s_["periode"] == n])
        ergebnis_manager.append({
            "id": mid, "name": state[mid]["name"].strip(), "ich": mid == kt.EIGENE_MANAGER_ID,
            **bereiche["Saison"],             # Saison-Werte auch direkt (kompatibel)
            "bereiche": bereiche,
            "positionen": details,
            "startkader_verkauft": sk,
        })

    # Bieter-Profil: wie viel OVP (in Euro und als Anteil am theoretischen Gewinn bis
    # Anpfiff) zahlt wer bei welchem MW-Anstieg der letzten Nacht (nur Kaeufe vom Markt)
    profil = []
    for lo, hi, name in ANSTIEG_STUFEN:
        g = [b for b in bieter_alle if b["anstieg"] is not None and lo <= b["anstieg"] < hi]
        eur = [b["ovp_eur"] for b in g]
        ant = [b["anteil"] for b in g if b["anteil"] is not None]
        zeile = {"stufe": name, "von": lo, "bis": hi, "anzahl": len(g),
                 "ovp_eur_median": quantil(eur, 0.5), "ovp_eur_p75": quantil(eur, 0.75),
                 "ovp_eur_max": max(eur, default=None),
                 "anteil_median": runde(quantil(ant, 0.5), 3), "anteil_p75": runde(quantil(ant, 0.75), 3),
                 "manager": {}}
        for mid in manager_ids:
            gm = [b for b in g if b["mid"] == mid]
            if gm:
                am = [b["anteil"] for b in gm if b["anteil"] is not None]
                zeile["manager"][mid] = {"anzahl": len(gm),
                                         "ovp_eur_median": quantil([b["ovp_eur"] for b in gm], 0.5),
                                         "ovp_eur_max": max(b["ovp_eur"] for b in gm),
                                         "anteil_median": runde(quantil(am, 0.5), 3),
                                         "anteil_p75": runde(quantil(am, 0.75), 3)}
        profil.append(zeile)

    return {
        "stand": jetzt.isoformat(),
        "mw_versatz": versatz, "mw_kalibrierung": kal, "mw_proben": len(proben),
        "anpfiffe": {str(t): a.isoformat() for t, a in anpfiffe},
        "manager": ergebnis_manager,
        "bieter_profil": profil,
        "perioden": [{"name": n, "start": st.isoformat()} for n, st in perioden],
        "bieter_kaeufe": [{k: b[k] for k in ("mid", "ovp_eur", "anstieg", "naechte", "anteil", "typ", "gewinn", "wt", "periode")}
                          for b in bieter_alle if b["ovp_eur"] is not None],
    }


def main() -> None:
    ergebnis = analysiere()
    alle_pos = [p for m in ergebnis["manager"] for p in m["positionen"]]
    fehlend = sum(1 for p in alle_pos if p["mw_kauf"] is None) / max(1, len(alle_pos))
    if fehlend > MAX_FEHLEND and os.path.exists(OUTPUT_FILE):
        print(f"WARNUNG: bei {fehlend:.0%} der Käufe fehlt der Marktwert (API-Drosselung?) - "
              f"{OUTPUT_FILE} bleibt unverändert.")
        sys.exit(0)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(ergebnis, f, ensure_ascii=False, separators=(",", ":"), default=str)
    for m in ergebnis["manager"]:
        print(f"{m['name']:14} Käufe {m['kaeufe']:3}  Trades {m['trades']['anzahl']:3}  "
              f"TF-Gewinn {m['tf_gewinn']:>13,.0f} €  OVP-Anteil am theor. Gewinn (Median) "
              f"{(m['trades']['anteil_median'] or 0) * 100:5.1f} %")
    print(f"{OUTPUT_FILE} geschrieben.")


if __name__ == "__main__":
    main()
