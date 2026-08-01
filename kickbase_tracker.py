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

# Beim Reset (after_reset=1) wird angenommen, dass Teamwert+Budget in Summe
# exakt diesem Wert entspricht (Kickbase-Standard: 100 Mio Startkader + 50 Mio
# Cash = 150 Mio Netto-Teamwert). Budget wird dann als 150 Mio - aktueller
# Teamwert bestimmt, statt geschaetzt zu werden.
NETTO_TEAMWERT_START = 150_000_000


# Startbudget pro Manager -> HIER die echten Werte deiner Liga eintragen.
# Key = manager_id (wird beim ersten Lauf ausgegeben, dann hier ergänzen).
# Fällt ein manager_id hier nicht rein, wird DEFAULT_START_BUDGET verwendet.
STARTBUDGETS = {
    # "abc123managerid": 50_000_000,
}
DEFAULT_START_BUDGET = 50_000_000

# Liga-Auswahl: leer lassen (None) -> nimmt automatisch die erste Liga.
# Sobald du mehrere Ligen hast (z.B. echte Liga + Testliga), hier die
# gewünschte League-ID eintragen (steht im Log als "Liga gefunden: NAME -> ID").
LEAGUE_ID_OVERRIDE = "11162077"  # z.B. "abc123..."


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
    ])

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
            # Budget exakt aus der Invariante Teamwert+Budget = 150 Mio ermitteln,
            # alle bisherigen Transfers/Historie verwerfen, Schwelle setzen -
            # ab jetzt zaehlen nur noch Transfers NACH diesem Zeitpunkt.
            budget = NETTO_TEAMWERT_START - teamwert
            day_result = compute_day(prev_budget=budget, teamwert=teamwert, netto_transfer=0.0)
            # compute_day addiert LOGIN_BONUS, den wollen wir beim Reset selbst nicht:
            day_result["budget"] = round(budget, 2)
            day_result["netto_teamwert"] = round(teamwert + budget, 2)
            basis = teamwert + min(budget, 0)
            day_result["max_gebot"] = round(budget + MINUS_GRENZE * basis, 2)
            day_result["date"] = target_day

            state[manager_id]["history"] = [day_result]
            state[manager_id]["reset_threshold"] = jetzt_iso
            print(f"[{name}] RESET: Budget neu berechnet = {budget:,.0f} € "
                  f"(Teamwert {teamwert:,.0f} €), Schwelle = {jetzt_iso}")
        else:
            reset_threshold = state[manager_id].get("reset_threshold")
            if history and history[-1]["date"] == target_day:
                # Tag ist noch "offen" (gleiches Zeitfenster, Fenster schließt erst
                # beim naechsten 22:04-Update) -> neu berechnen, NICHT ueberspringen,
                # damit neue Transfers seit dem letzten Lauf erfasst werden.
                netto_transfer = get_netto_transfer_am_tag(transfers_store, manager_id, target_day, reset_threshold)
                prev_budget = history[-2]["budget"] if len(history) >= 2 else start_budget
                day_result = compute_day(prev_budget, teamwert, netto_transfer)
                day_result["date"] = target_day
                history[-1] = day_result  # bestehenden (noch offenen) Eintrag ueberschreiben
                print(f"[{name}] {target_day}: Eintrag war schon offen, neu berechnet/aktualisiert.")
            else:
                netto_transfer = get_netto_transfer_am_tag(transfers_store, manager_id, target_day, reset_threshold)
                prev_budget = last_budget(history, start_budget)

                day_result = compute_day(prev_budget, teamwert, netto_transfer)
                day_result["date"] = target_day

                history.append(day_result)

        zeilen.append({"Manager": name, **day_result})

    if after_reset:
        config["after_reset"] = 0
        save_config(config)
        print("=== AFTER_RESET abgeschlossen, config.json auf after_reset=0 zurückgesetzt ===")

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
