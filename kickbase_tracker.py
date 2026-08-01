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
MINUS_GRENZE = 1 / 3        # offizielle Kickbase 33%-Regel

STATE_FILE = "state.json"

# Startbudget pro Manager -> HIER die echten Werte deiner Liga eintragen.
# Key = manager_id (wird beim ersten Lauf ausgegeben, dann hier ergänzen).
# Fällt ein manager_id hier nicht rein, wird DEFAULT_START_BUDGET verwendet.
STARTBUDGETS = {
    # "abc123managerid": 50_000_000,
}
DEFAULT_START_BUDGET = 50_000_000


# ---------------------------------------------------------------------------
# 2) API-ZUGRIFF
# ---------------------------------------------------------------------------

def login(email: str, password: str) -> str:
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


def get_first_league_id(headers: dict) -> str:
    url = "https://api.kickbase.com/v4/leagues"
    r = requests.get(url, headers=headers)
    r.raise_for_status()
    leagues = r.json()["lins"]
    for l in leagues:
        print(f"Liga gefunden: {l['n']} -> {l['i']}")
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
    """Eindeutiger Schlüssel pro Transfer, um Duplikate beim Mergen zu erkennen."""
    return f"{t['dt']}|{t['pn']}|{t['trp']}|{t['tty']}"


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


def get_tagesgewinn_aus_archiv(store: dict, manager_id: str) -> pd.DataFrame:
    """Netto-Transfer (Gewinn) pro Kickbase-Tag, berechnet aus dem GESAMTEN Archiv
    (nicht nur den aktuell von der API sichtbaren letzten ~24 Transfers)."""
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
    ])

    df["Tag"] = df["Datetime"].apply(kickbase_day)
    df["Tag"] = pd.to_datetime(df["Tag"]) + pd.Timedelta(days=1)
    df["Gewinn"] = df.apply(
        lambda x: x["Preis"] if x["Aktion"] == "verkauft" else -x["Preis"],
        axis=1,
    )

    return df.groupby("Tag")["Gewinn"].sum().reset_index()


def get_netto_transfer_am_tag(store: dict, manager_id: str, target_day: str) -> float:
    """Netto-Transfer eines Managers für genau einen Kickbase-Tag (0, falls kein Transfer)."""
    tagesgewinne = get_tagesgewinn_aus_archiv(store, manager_id)
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


def last_budget(history: list, start_budget: float) -> float:
    if not history:
        return start_budget
    return history[-1]["budget"]


def compute_day(prev_budget: float, teamwert: float, netto_transfer: float) -> dict:
    budget = prev_budget + netto_transfer + LOGIN_BONUS
    netto_teamwert = teamwert + budget
    max_gebot = budget + MINUS_GRENZE * netto_teamwert
    return {
        "budget": round(budget, 2),
        "teamwert": round(teamwert, 2),
        "netto_transfer": round(netto_transfer, 2),
        "netto_teamwert": round(netto_teamwert, 2),
        "max_gebot": round(max_gebot, 2),
    }


# ---------------------------------------------------------------------------
# 4) HAUPTABLAUF
# ---------------------------------------------------------------------------

def run_for_day(target_day: str) -> pd.DataFrame:
    print("Login bei Kickbase ...")
    token = login(KICKBASE_EMAIL, KICKBASE_PASSWORD)
    headers = auth_headers(token)

    league_id = get_first_league_id(headers)
    managers = get_managers(league_id, headers)

    state = load_state()
    transfers_store = load_transfers_store()
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
            state[manager_id] = {"name": name, "start_budget": start_budget, "history": []}

        history = state[manager_id]["history"]

        if history and history[-1]["date"] == target_day:
            print(f"[{name}] {target_day} bereits vorhanden, überspringe.")
            zeilen.append({"Manager": name, **history[-1]})
            continue

        netto_transfer = get_netto_transfer_am_tag(transfers_store, manager_id, target_day)
        prev_budget = last_budget(history, start_budget)

        day_result = compute_day(prev_budget, teamwert, netto_transfer)
        day_result["date"] = target_day

        history.append(day_result)
        zeilen.append({"Manager": name, **day_result})

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


if __name__ == "__main__":
    main()
