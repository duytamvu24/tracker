"""
Kickbase Budget- & Max-Gebot-Tracker – Komplettskript
=======================================================

Was das Skript macht (einmal pro Tag ausführen, z.B. per Cronjob nach 22 Uhr):

1. Login bei Kickbase, Token holen
2. Liga + alle Manager der Liga ermitteln (Ranking-Endpoint)
3. Für jeden Manager: aktuellen Teamwert (Dashboard) + Transfers der letzten Tage abrufen
4. Transfers pro Manager auf Kickbase-Tage aggregieren (Grenze 22:04 Uhr) -> Netto-Transfer
5. Für den Ziel-Tag: Budget(t) = Budget(t-1) + Netto-Transfer(t) + Login-Bonus
                     Netto-Teamwert(t) = Teamwert(t) + Budget(t)
                     Max-Gebot(t) = Budget(t) + 1/3 * Netto-Teamwert(t)
6. Ergebnis wird in state.json gespeichert (Historie je Manager) und als Tabelle ausgegeben

WICHTIG, bevor es läuft:
- Zugangsdaten unten eintragen (oder besser: als Umgebungsvariablen setzen, siehe unten)
- STARTBUDGETS unten mit den echten Startbudgets deiner Liga-Mitspieler befüllen
  (die kennt die API nicht -> muss einmalig von dir eingetragen werden)
- Der Transfer-Endpoint liefert laut Kickbase nur die letzten ~24 Transfers pro Manager.
  Für den täglichen Lauf reicht das. Für eine rückwirkende Komplett-Historie seit
  Saisonstart müsstest du prüfen, ob es Pagination gibt.
- Teamwert kommt aus dem Dashboard-Endpoint = IMMER der aktuelle Wert. D.h. rückwirkend
  lassen sich alte Tage nur berechnen, wenn du das Skript an dem Tag auch wirklich
  ausgeführt und den Wert gespeichert hast.

Nutzung:
    python kickbase_tracker.py                     # heutiger Kickbase-Tag
    python kickbase_tracker.py --date 2026-08-05    # bestimmter Tag
"""

import os
import sys
import json
import argparse
from datetime import date, datetime

import requests
import pandas as pd

# ---------------------------------------------------------------------------
# 1) KONFIGURATION
# ---------------------------------------------------------------------------

# Zugangsdaten: bevorzugt über Umgebungsvariablen setzen, NICHT im Klartext
# committen. Falls du sie direkt eintragen willst, ersetze os.environ.get(...)
# durch den String selbst.
KICKBASE_EMAIL = os.environ.get("KICKBASE_EMAIL", "DEINE_EMAIL")
KICKBASE_PASSWORD = os.environ.get("KICKBASE_PASSWORD", "DEIN_PASSWORT")

LOGIN_BONUS = 100_000       # € pro Tag, laut eurer Liga-Regel fix
MINUS_GRENZE = 0.33         # exakt 33,00% - empirisch bestaetigt (Abweichung nur 1 Euro
                             # bei Budget=50.152.491 / Teamwert=99.847.509 -> Max-Gebot
                             # exakt 83.102.169, real getestet: 83.102.168)

STATE_FILE = "state.json"
CONFIG_FILE = "config.json"


# Startbudget pro Manager -> HIER die echten Werte deiner Liga eintragen.
# Key = manager_id (wird beim ersten Lauf ausgegeben, dann hier ergänzen).
# Fällt ein manager_id hier nicht rein, wird DEFAULT_START_BUDGET verwendet.
STARTBUDGETS = {
    # "abc123managerid": 50_000_000,
}
DEFAULT_START_BUDGET = 50_000_000

# Korrigierte Startbudgets fuer die LAUFENDE Saison (nach dem Reset).
# Diese Werte ueberschreiben das beim Reset geschaetzte start_budget in state.json.
# Die komplette Budget-Kette wird danach automatisch neu berechnet.
# Ermittelt aus dem Abgleich mit dem echten App-Budget am 06.10.2026:
#   Duy Tam:     Tracker -45.212.629 / App -40.259.629 -> +4.953.000
#   Dragontrieu: Tracker -49.116.123 / App -45.716.123 -> +3.400.000
# NACH DEM NAECHSTEN SAISON-RESET LEEREN!
START_BUDGET_OVERRIDES = {
    "3310917": 54_953_000,   # Duy Tam
    "3458038": 53_400_000,   # Dragontrieu
}

# Deine eigene Manager-ID. Fuer dich liefert die API das ECHTE Budget
# (/me/budget) - der Tracker vergleicht es jeden Lauf mit seiner Berechnung
# und schreibt das Ergebnis als "budget_abgleich" in state.json.
EIGENE_MANAGER_ID = "3310917"   # Duy Tam

# Ziel-Kaderwert fuer den Reset: liegt der zufaellig ausgeloste Startkader
# darunter, wird der Fehlbetrag automatisch aufs Startbudget draufgelegt.
SQUAD_TARGET = 100_000_000

# Liga-Auswahl: leer lassen (None) -> nimmt automatisch die erste Liga.
# Sobald du mehrere Ligen hast (z.B. echte Liga + Testliga), hier die
# gewünschte League-ID eintragen (steht im Log als "Liga gefunden: NAME -> ID").
LEAGUE_ID_OVERRIDE = "5819867"  # Testliga ("test"). Echte Liga (ESY KICKBASE) waere "5819867"


# ---------------------------------------------------------------------------
# 2) API-ZUGRIFF
# ---------------------------------------------------------------------------

def login(email: str, password: str) -> str:
    if email in ("DEINE_EMAIL", "") or password in ("DEIN_PASSWORT", ""):
        raise RuntimeError(
            "KICKBASE_EMAIL / KICKBASE_PASSWORD sind nicht gesetzt (noch Platzhalter). "
            "Bei GitHub Actions: Settings -> Secrets and variables -> Actions pruefen, "
            "ob beide Secrets exakt so benannt und befuellt sind. Lokal: als Umgebungs-"
            "variablen setzen."
        )
    url = "https://api.kickbase.com/v4/user/login"
    data = {"em": email, "pass": password, "loy": False}
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    r = requests.post(url, data=json.dumps(data), headers=headers)
    r.raise_for_status()
    return r.json()["tkn"]


def auth_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": "Kickbase/4.0.0",
    }


def get_league_id(headers: dict) -> str:
    url = "https://api.kickbase.com/v4/leagues"
    r = requests.get(url, headers=headers)
    r.raise_for_status()
    leagues = r.json()["lins"]
    for l in leagues:
        print(f"Liga gefunden: {l['n']} -> {l['i']}")

    if LEAGUE_ID_OVERRIDE:
        target = str(LEAGUE_ID_OVERRIDE).strip()
        for l in leagues:
            if str(l["i"]).strip() == target:
                print(f"-> nutze konfigurierte Liga: {l['n']}")
                return l["i"]
        raise RuntimeError(
            f"LEAGUE_ID_OVERRIDE='{LEAGUE_ID_OVERRIDE}' wurde nicht unter deinen Ligen gefunden."
        )

    print(f"-> nutze erste gefundene Liga: {leagues[0]['n']} (keine LEAGUE_ID_OVERRIDE gesetzt)")
    return leagues[0]["i"]


def get_managers(league_id: str, headers: dict) -> list[dict]:
    """Liste aller Manager der Liga inkl. aktuellem Teamwert (Dashboard)."""
    url = f"https://api.kickbase.com/v4/leagues/{league_id}/ranking"
    r = requests.get(url, headers=headers)
    r.raise_for_status()

    managers = []
    for m in r.json()["us"]:
        manager_id = m["i"]
        name = m["n"]

        dash_url = f"https://api.kickbase.com/v4/leagues/{league_id}/managers/{manager_id}/dashboard"
        r_dash = requests.get(dash_url, headers=headers)
        r_dash.raise_for_status()
        dashboard = r_dash.json()
        team_value = dashboard["tv"]  # Key ggf. anpassen, falls sich die API ändert

        managers.append({"manager_id": manager_id, "name": name, "teamwert": team_value})

    return managers


def get_eigenes_budget(league_id: str, headers: dict) -> float | None:
    """Echtes Budget des eingeloggten Users (GET /v4/leagues/{id}/me/budget -> "b").
    Gibt None zurueck, falls der Abruf scheitert - der Tracker laeuft dann normal weiter."""
    try:
        url = f"https://api.kickbase.com/v4/leagues/{league_id}/me/budget"
        r = requests.get(url, headers=headers, timeout=20)
        r.raise_for_status()
        return float(r.json()["b"])
    except Exception as e:  # noqa: BLE001 - reiner Zusatz-Check, darf nie den Lauf abbrechen
        print(f"Hinweis: eigenes Budget (/me/budget) nicht abrufbar: {e}")
        return None


def kickbase_day(dt: pd.Timestamp):
    """Ordnet einen Zeitstempel dem Kickbase-Tag zu (Grenze 22:04 Uhr)."""
    if dt.time() < pd.Timestamp("22:04").time():
        return (dt - pd.Timedelta(days=1)).date()
    else:
        return dt.date()


def aktueller_kickbase_tag() -> str:
    """Bestimmt automatisch den aktuellen Kickbase-Tag (Berlin-Zeit, Grenze 22:04 Uhr) -
    dieselbe Logik wie bei der Transfer-Zuordnung, damit Teamwert/Transfer/Budget
    konsistent demselben Tag zugeordnet werden."""
    jetzt = pd.Timestamp.now(tz="Europe/Berlin")
    tag = kickbase_day(jetzt)
    tag = pd.Timestamp(tag) + pd.Timedelta(days=1)
    return tag.date().isoformat()


# ---------------------------------------------------------------------------
# Persistentes Transfer-Archiv
# ---------------------------------------------------------------------------
# Problem: der /transfer-Endpoint liefert nur die letzten ~24 Einträge pro
# Manager. Fällt ein Transfer da raus, ist er über die API nicht mehr sichtbar.
# Lösung: bei jedem Lauf alle aktuell sichtbaren Transfers in eine eigene,
# dauerhafte Datei mergen (Dedupliziert über einen eindeutigen Schlüssel).
# Einmal gespeichert, geht ein Transfer nie wieder verloren - unabhängig
# davon, ob die API ihn später noch zeigt oder nicht.
TRANSFERS_STORE_FILE = "transfers_store.json"


def load_transfers_store() -> dict:
    if os.path.exists(TRANSFERS_STORE_FILE):
        with open(TRANSFERS_STORE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_transfers_store(store: dict) -> None:
    with open(TRANSFERS_STORE_FILE, "w", encoding="utf-8") as f:
        json.dump(store, f, indent=2, ensure_ascii=False)


def _transfer_key(t: dict) -> str:
    """Eindeutiger Schlüssel pro Transfer, um Duplikate beim Mergen zu erkennen.

    Bewusst OHNE Spielername: Kickbase aendert manchmal die Schreibweise
    (z.B. "Maksimovic" -> "Maksimović", "Guiu" -> "Marc Guiu"). Mit Namen im
    Schluessel wurde derselbe Transfer dann ein zweites Mal archiviert.
    Zeitstempel (sekundengenau) + Preis + Typ ist pro Manager eindeutig genug."""
    return f"{t['dt']}|{t['trp']}|{t['tty']}"


def dedupe_transfers_store(store: dict) -> None:
    """Bereinigt das Archiv einmalig von Duplikaten, die durch Namensaenderungen
    entstanden sind (alter Schluessel enthielt den Namen). Idempotent - kann
    bei jedem Lauf aufgerufen werden."""
    for manager_id, manager_store in store.items():
        bereinigt = {}
        for t in manager_store.values():
            key = _transfer_key(t)
            if key in bereinigt:
                # Duplikat: einen gesetzten "excluded"-Toggle uebernehmen
                if t.get("excluded"):
                    bereinigt[key]["excluded"] = True
                print(f"  -> Duplikat entfernt (Manager {manager_id}): "
                      f"{t['dt']} {t['pn']} {t['trp']:,} € (tty {t['tty']}), "
                      f"behalten als '{bereinigt[key]['pn']}'")
                continue
            bereinigt[key] = dict(t)
        store[manager_id] = bereinigt


def fetch_and_archive_transfers(league_id: str, manager_id: str, headers: dict, store: dict) -> None:
    """Holt die aktuell sichtbaren (letzten ~24) Transfers und merged sie ins Archiv."""
    url = f"https://api.kickbase.com/v4/leagues/{league_id}/managers/{manager_id}/transfer"
    r = requests.get(url, headers=headers)
    r.raise_for_status()
    transfers = r.json().get("it", [])

    manager_store = store.setdefault(manager_id, {})
    neu = 0
    for t in transfers:
        key = _transfer_key(t)
        if key not in manager_store:
            manager_store[key] = {"dt": t["dt"], "pn": t["pn"], "trp": t["trp"], "tty": t["tty"]}
            neu += 1
    if neu:
        print(f"  -> {neu} neue Transfer(s) fürs Archiv gefunden (Manager {manager_id}).")


def get_tagesgewinn_aus_archiv(store: dict, manager_id: str, reset_threshold: str | None = None) -> pd.DataFrame:
    """Netto-Transfer (Gewinn) pro Kickbase-Tag, berechnet aus dem GESAMTEN Archiv
    (nicht nur den aktuell von der API sichtbaren letzten ~24 Transfers).
    Transfers VOR reset_threshold (falls gesetzt) werden ignoriert."""
    manager_store = store.get(manager_id, {})
    if not manager_store:
        return pd.DataFrame(columns=["Tag", "Gewinn"])

    df = pd.DataFrame([
        {
            "Datetime": pd.to_datetime(t["dt"]).tz_convert("Europe/Berlin"),
            "Preis": t["trp"],
            "Aktion": "gekauft" if t["tty"] == 1 else "verkauft",
        }
        for t in manager_store.values()
        if not t.get("excluded", False)  # ausgeschlossene Transfers (Toggle) ignorieren
    ])
    if df.empty:
        return pd.DataFrame(columns=["Tag", "Gewinn"])

    if reset_threshold:
        schwelle = pd.to_datetime(reset_threshold)
        if schwelle.tzinfo is None:
            schwelle = schwelle.tz_localize("Europe/Berlin")
        else:
            schwelle = schwelle.tz_convert("Europe/Berlin")
        df = df[df["Datetime"] > schwelle]
        if df.empty:
            return pd.DataFrame(columns=["Tag", "Gewinn"])

    df["Tag"] = df["Datetime"].apply(kickbase_day)
    df["Tag"] = pd.to_datetime(df["Tag"]) + pd.Timedelta(days=1)
    df["Gewinn"] = df.apply(
        lambda x: x["Preis"] if x["Aktion"] == "verkauft" else -x["Preis"],
        axis=1,
    )

    return df.groupby("Tag")["Gewinn"].sum().reset_index()


def get_netto_transfer_am_tag(store: dict, manager_id: str, target_day: str, reset_threshold: str | None = None) -> float:
    """Netto-Transfer eines Managers für genau einen Kickbase-Tag (0, falls kein Transfer)."""
    tagesgewinne = get_tagesgewinn_aus_archiv(store, manager_id, reset_threshold)
    target_ts = pd.to_datetime(target_day)
    treffer = tagesgewinne[tagesgewinne["Tag"] == target_ts]
    if treffer.empty:
        return 0.0
    return float(treffer["Gewinn"].iloc[0])


# ---------------------------------------------------------------------------
# 3) BUDGET-/MAX-GEBOT-BERECHNUNG + STATE
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def load_config() -> dict:
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"after_reset": 0}


def save_config(config: dict) -> None:
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)


def last_budget(history: list, start_budget: float) -> float:
    if not history:
        return start_budget
    return history[-1]["budget"]


def compute_day(prev_budget: float, teamwert: float, netto_transfer: float) -> dict:
    budget = prev_budget + netto_transfer + LOGIN_BONUS
    netto_teamwert = teamwert + budget

    # Basis fuer die 33%-Regel: negatives Budget reduziert die Basis (offizielles
    # Kickbase-Beispiel: Teamwert 100 + Kontostand -10 = 90), aber POSITIVES Budget
    # zaehlt NICHT extra dazu - empirisch bestaetigt (Budget ~58,3 Mio, Teamwert
    # ~92,7 Mio -> reale Grenze ~89 Mio, passt nur zu "Teamwert + min(Budget,0)",
    # nicht zu "Teamwert + Budget").
    basis = teamwert + min(budget, 0)
    max_gebot = budget + MINUS_GRENZE * basis

    return {
        "budget": round(budget, 2),
        "teamwert": round(teamwert, 2),
        "netto_transfer": round(netto_transfer, 2),
        "netto_teamwert": round(netto_teamwert, 2),
        "max_gebot": round(max_gebot, 2),
    }


def recompute_history(history: list, start_budget: float, store: dict,
                      manager_id: str, reset_threshold: str | None) -> None:
    """Berechnet die KOMPLETTE Budget-Kette ab Startbudget neu aus dem Archiv.

    Vorher wurde nur der jeweils letzte Eintrag nachkorrigiert. Taucht ein
    Transfer erst spaeter im Archiv auf (oder wird ein Startbudget korrigiert),
    wurden aeltere Tage nie aktualisiert und der Fehler lief dauerhaft mit.
    Der gespeicherte Teamwert je Tag bleibt unveraendert (der ist nicht
    rueckwirkend abrufbar)."""
    tagesgewinne = get_tagesgewinn_aus_archiv(store, manager_id, reset_threshold)
    netto_je_tag = {
        pd.Timestamp(row.Tag).date().isoformat(): float(row.Gewinn)
        for row in tagesgewinne.itertuples(index=False)
    }

    budget = start_budget
    for eintrag in history:
        netto = netto_je_tag.get(eintrag["date"], 0.0)
        neu = compute_day(budget, eintrag["teamwert"], netto)
        neu["date"] = eintrag["date"]
        eintrag.update(neu)
        budget = neu["budget"]


# ---------------------------------------------------------------------------
# 4) HAUPTABLAUF
# ---------------------------------------------------------------------------

def run_for_day(target_day: str) -> pd.DataFrame:
    print("Login bei Kickbase ...")
    token = login(KICKBASE_EMAIL, KICKBASE_PASSWORD)
    headers = auth_headers(token)

    league_id = get_league_id(headers)
    managers = get_managers(league_id, headers)

    state = load_state()
    transfers_store = load_transfers_store()
    dedupe_transfers_store(transfers_store)
    config = load_config()
    after_reset = config.get("after_reset", 0) == 1
    if after_reset:
        print("=== AFTER_RESET aktiv: setze Budget/Teamwert-Basis für alle Manager neu ===")

    jetzt_iso = pd.Timestamp.now(tz="Europe/Berlin").isoformat()

    zeilen = []

    for m in managers:
        manager_id = m["manager_id"]
        name = m["name"]
        teamwert = m["teamwert"]

        # Immer zuerst archivieren, auch wenn der Tag schon berechnet war -
        # so verlierst du nie Transfers, egal wann du das Skript laufen lässt.
        fetch_and_archive_transfers(league_id, manager_id, headers, transfers_store)

        start_budget = STARTBUDGETS.get(manager_id, DEFAULT_START_BUDGET)
        if manager_id not in STARTBUDGETS:
            print(f"WARNUNG: kein Startbudget für '{name}' ({manager_id}) hinterlegt, "
                  f"nutze Default {DEFAULT_START_BUDGET:,.0f} €. In STARTBUDGETS ergänzen!")

        if manager_id not in state:
            state[manager_id] = {"name": name, "start_budget": start_budget, "history": [], "reset_threshold": None}
        if "reset_threshold" not in state[manager_id]:
            state[manager_id]["reset_threshold"] = None

        history = state[manager_id]["history"]

        if after_reset:
            # Beobachtung/Hypothese: Liegt der zufaellig ausgeloste Startkader
            # UNTER dem Zielwert (SQUAD_TARGET, standardmaessig 100 Mio), wird
            # der Fehlbetrag automatisch aufs Budget draufgelegt (Ausgleich auf
            # insgesamt 150 Mio bei 50 Mio Startbudget). Liegt der Kader DARUEBER,
            # bleibt es beim normalen, gewaehlten Startbudget (kein Abzug).
            # ACHTUNG: In der Saison 2026/27 lag diese Hypothese bei Duy Tam und
            # Dragontrieu daneben (beide bekamen 50 Mio, real waren es mehr).
            # -> nach jedem Reset echte Startbudgets pruefen und ggf. in
            #    START_BUDGET_OVERRIDES eintragen.
            fehlbetrag = max(0.0, SQUAD_TARGET - teamwert)
            budget = start_budget + fehlbetrag
            day_result = compute_day(prev_budget=budget, teamwert=teamwert, netto_transfer=0.0)
            # compute_day addiert LOGIN_BONUS, den wollen wir beim Reset selbst nicht:
            day_result["budget"] = round(budget, 2)
            day_result["netto_teamwert"] = round(teamwert + budget, 2)
            basis = teamwert + min(budget, 0)
            day_result["max_gebot"] = round(budget + MINUS_GRENZE * basis, 2)
            day_result["date"] = target_day

            state[manager_id]["history"] = [day_result]
            state[manager_id]["reset_threshold"] = jetzt_iso
            state[manager_id]["start_budget"] = budget  # tatsaechlicher (ggf. aufgestockter) Wert merken
            state[manager_id]["teamwert_bei_reset"] = teamwert  # fuer spaetere Pruefung der Reset-Regel
            if fehlbetrag > 0:
                print(f"[{name}] RESET: Teamwert {teamwert:,.0f} € < {SQUAD_TARGET:,.0f} € -> "
                      f"Fehlbetrag {fehlbetrag:,.0f} € aufs Budget draufgelegt. "
                      f"Budget = {budget:,.0f} €, Schwelle = {jetzt_iso}")
            else:
                print(f"[{name}] RESET: Teamwert {teamwert:,.0f} € >= {SQUAD_TARGET:,.0f} € -> "
                      f"normales Startbudget = {budget:,.0f} €, Schwelle = {jetzt_iso}")
        else:
            reset_threshold = state[manager_id].get("reset_threshold")

            # Korrigiertes Startbudget (aus Abgleich mit der App) uebernehmen
            if manager_id in START_BUDGET_OVERRIDES:
                korrigiert = float(START_BUDGET_OVERRIDES[manager_id])
                alt = state[manager_id].get("start_budget")
                if alt is None or abs(alt - korrigiert) > 0.01:
                    print(f"[{name}] Startbudget korrigiert: {alt or 0:,.0f} € -> {korrigiert:,.0f} €")
                    state[manager_id]["start_budget"] = korrigiert

            effektives_start_budget = state[manager_id].get("start_budget", start_budget)

            # Fehlende Tage (Skript nicht gelaufen) bzw. den heutigen Tag als
            # Eintrag anlegen. Die Budget-Werte werden danach ohnehin komplett
            # neu berechnet, hier geht es nur um die Tages-Eintraege + Teamwert.
            if history and history[-1]["date"] == target_day:
                history[-1]["teamwert"] = teamwert  # offener Tag -> aktuellen Teamwert nehmen
            else:
                if history:
                    letzter_tag = pd.to_datetime(history[-1]["date"])
                    nachzuholende_tage = pd.date_range(
                        start=letzter_tag + pd.Timedelta(days=1),
                        end=target_day,
                    )
                else:
                    nachzuholende_tage = pd.to_datetime([target_day])

                if len(nachzuholende_tage) > 1:
                    print(f"[{name}] Hole {len(nachzuholende_tage) - 1} übersprungene(n) Tag(e) nach "
                          f"({nachzuholende_tage[0].date()} bis {nachzuholende_tage[-2].date()}).")

                for tag_ts in nachzuholende_tage:
                    # Fuer uebersprungene Vergangenheits-Tage ist der ECHTE historische
                    # Teamwert nicht mehr abrufbar -> aktueller Wert als Naeherung
                    # (betrifft nur Netto-Teamwert/Max-Gebot, nicht die Budget-Kette).
                    history.append({"date": tag_ts.date().isoformat(), "teamwert": teamwert})

            # Komplette Kette neu berechnen: erfasst auch Transfers, die erst
            # spaeter im Archiv aufgetaucht sind, und korrigierte Startbudgets.
            alte_budgets = {e["date"]: e.get("budget") for e in history}
            recompute_history(history, effektives_start_budget, transfers_store,
                              manager_id, reset_threshold)
            geaendert = [
                e for e in history
                if alte_budgets.get(e["date"]) is not None
                and abs(alte_budgets[e["date"]] - e["budget"]) > 0.01
            ]
            if geaendert:
                erster = geaendert[0]
                print(f"[{name}] Budget-Kette ab {erster['date']} korrigiert "
                      f"({len(geaendert)} Tag(e), heute jetzt {history[-1]['budget']:,.0f} €).")

            day_result = history[-1]

        zeilen.append({"Manager": name, **day_result})

    if after_reset:
        config["after_reset"] = 0
        save_config(config)
        print("=== AFTER_RESET abgeschlossen, config.json auf after_reset=0 zurückgesetzt ===")

    # Abgleich: eigenes ECHTES Budget vs. Tracker-Berechnung. Bewusst nur
    # protokolliert (nicht automatisch korrigiert), damit eine laufende
    # Abweichung sichtbar bleibt statt still weggerechnet zu werden.
    if EIGENE_MANAGER_ID in state and state[EIGENE_MANAGER_ID]["history"]:
        echt = get_eigenes_budget(league_id, headers)
        if echt is not None:
            berechnet = state[EIGENE_MANAGER_ID]["history"][-1]["budget"]
            diff = echt - berechnet
            state[EIGENE_MANAGER_ID]["budget_abgleich"] = {
                "date": state[EIGENE_MANAGER_ID]["history"][-1]["date"],
                "geprueft_um": jetzt_iso,
                "echt": echt,
                "tracker": berechnet,
                "differenz": round(diff, 2),
            }
            if abs(diff) <= 1:
                print(f"Budget-Abgleich OK: Tracker = App = {echt:,.0f} €")
            else:
                print(f"WARNUNG Budget-Abgleich: App {echt:,.0f} € vs. Tracker {berechnet:,.0f} € "
                      f"-> Differenz {diff:+,.0f} €. (Login-Bonus des neuen Tages evtl. noch "
                      f"nicht abgeholt, sonst Startbudget/Transfers pruefen.)")

    save_state(state)
    save_transfers_store(transfers_store)

    overview = pd.DataFrame(zeilen)[
        ["Manager", "date", "teamwert", "netto_transfer", "budget", "netto_teamwert", "max_gebot"]
    ].rename(columns={
        "date": "Tag",
        "teamwert": "Teamwert",
        "netto_transfer": "Netto-Transfer",
        "budget": "Budget",
        "netto_teamwert": "Netto-Teamwert",
        "max_gebot": "Max-Gebot",
    })

    return overview


def schreibe_dashboard(overview: pd.DataFrame, target_day: str) -> None:
    """Schreibt eine README.md mit einer Uebersichtstabelle, die direkt in GitHub
    (Web und App) beim Oeffnen des Repos angezeigt wird - kein Klicken durch
    Logs oder rohe JSON-Dateien noetig."""
    jetzt = pd.Timestamp.now(tz="Europe/Berlin").strftime("%d.%m.%Y %H:%M Uhr")

    sortiert = overview.sort_values("Max-Gebot", ascending=False).copy()
    for spalte in ["Teamwert", "Netto-Transfer", "Budget", "Netto-Teamwert", "Max-Gebot"]:
        sortiert[spalte] = sortiert[spalte].map(lambda x: f"{x:,.0f} €".replace(",", "."))

    zeilen_md = ["| Manager | Teamwert | Netto-Transfer | Budget | Netto-Teamwert | Max-Gebot |",
                 "|---|---:|---:|---:|---:|---:|"]
    for _, row in sortiert.iterrows():
        zeilen_md.append(
            f"| {row['Manager']} | {row['Teamwert']} | {row['Netto-Transfer']} | "
            f"{row['Budget']} | {row['Netto-Teamwert']} | {row['Max-Gebot']} |"
        )

    inhalt = f"""# Kickbase Tracker

**Kickbase-Tag:** {target_day}
**Zuletzt aktualisiert:** {jetzt}

{chr(10).join(zeilen_md)}

---
*Wird automatisch taeglich per GitHub Actions aktualisiert. Werte fuer andere
Manager als dich selbst sind Schaetzungen (siehe Login-Bonus-Einschraenkung).*
"""

    with open("README.md", "w", encoding="utf-8") as f:
        f.write(inhalt)


def main():
    parser = argparse.ArgumentParser(description="Kickbase Budget- & Max-Gebot-Tracker")
    parser.add_argument(
        "--date",
        default=None,
        help="Kickbase-Tag im Format YYYY-MM-DD. Ohne Angabe: automatisch der aktuelle "
             "Kickbase-Tag (heute, unter Berücksichtigung der 22:04-Uhr-Grenze).",
    )
    args, _unknown = parser.parse_known_args()

    target_day = args.date if args.date else aktueller_kickbase_tag()

    try:
        datetime.strptime(target_day, "%Y-%m-%d")
    except ValueError:
        print("Fehler: --date muss im Format YYYY-MM-DD sein, z.B. 2026-08-05")
        sys.exit(1)

    print(f"Ziel-Tag: {target_day}")
    overview = run_for_day(target_day)
    print("\n=== Übersicht ===")
    print(overview.to_string(index=False))
    schreibe_dashboard(overview, target_day)
    print("\nREADME.md aktualisiert.")


if __name__ == "__main__":
    main()
