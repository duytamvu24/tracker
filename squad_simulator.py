"""
Kickbase Kader-Simulator ("Was-waere-wenn Spieler X entfaellt")
================================================================

Zeigt den kompletten Kader eines Managers mit Marktwert pro Spieler und
simuliert, was passiert, wenn ein oder mehrere Spieler entfernt werden
(als haette man sie exakt zum Marktwert verkauft):

    Neuer Teamwert = Teamwert - Summe(entfernte Marktwerte)
    Neues Budget   = Budget   + Summe(entfernte Marktwerte)
    Neues Max-Gebot = neu berechnet mit der 33%-Regel

Feldnamen sind empirisch bestaetigt (siehe Kommentare in get_squad()):
pi=Spieler-ID, pn=Name, pos=Position (1=TW,2=ABW,3=MF,4=ST), mv=Marktwert,
tfhmvt=Marktwert-Aenderung des letzten Updates.

Nutzung (in Jupyter):

    from squad_simulator import login_and_get_squad, list_squad, simulate

    login_and_get_squad(manager_id="3310917")   # holt Kader einmalig, cached lokal
    list_squad("3310917")                       # zeigt Kader mit Index + Marktwert
    simulate("3310917", entferne_indices=[2, 5])  # Spieler #2 und #5 simulieren
"""

import json
import os
import requests

import kickbase_tracker as kt

SQUAD_CACHE_FILE = "squad_cache.json"

# Bestaetigt anhand echter Beispieldaten (Spielername -> bekannte Position
# gegengecheckt, z.B. Goretzka=MF, Pervan/Dahmen=TW, Tietz/Pieringer=ST).
POSITION_MAP = {1: "TW", 2: "ABW", 3: "MF", 4: "ST"}


def get_squad(league_id: str, manager_id: str, headers: dict) -> list[dict]:
    """
    Holt den Kader eines Managers mit Marktwert, Position und letzter
    Marktwert-Aenderung pro Spieler.
    Bestaetigte Struktur: GET /v4/leagues/{leagueId}/managers/{managerId}/squad
    -> {"it": [{"pi": id, "pn": Name, "pos": 1-4, "mv": Marktwert,
                "tfhmvt": letzte Marktwert-Aenderung, ...}, ...]}

    Feld-Bestaetigung (empirisch): "tfhmvt" ist die Marktwert-Aenderung des
    letzten Updates - das Vorzeichen stimmt bei allen Testdaten exakt mit
    "mvt" (0=neutral, 1=steigend, 2=fallend) ueberein.
    """
    url = f"https://api.kickbase.com/v4/leagues/{league_id}/managers/{manager_id}/squad"
    r = requests.get(url, headers=headers)
    r.raise_for_status()
    data = r.json()

    rohliste = data.get("it", [])
    spieler = []
    for p in rohliste:
        spieler.append({
            "id": p.get("pi"),
            "name": p["pn"],
            "marktwert": p["mv"],
            "position": POSITION_MAP.get(p.get("pos"), "?"),
            "mw_aenderung": p.get("tfhmvt", 0),
        })
    return spieler


def load_squad_cache() -> dict:
    if os.path.exists(SQUAD_CACHE_FILE):
        with open(SQUAD_CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_squad_cache(cache: dict) -> None:
    with open(SQUAD_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2, ensure_ascii=False)


def login_and_get_squad(manager_id: str) -> None:
    """Loggt sich ein, holt den Kader des Managers und cached ihn lokal (squad_cache.json)."""
    token = kt.login(kt.KICKBASE_EMAIL, kt.KICKBASE_PASSWORD)
    headers = kt.auth_headers(token)
    league_id = kt.get_league_id(headers)

    spieler = get_squad(league_id, manager_id, headers)
    if not spieler:
        print(f"Keine Spieler gefunden fuer Manager {manager_id} - Endpunkt/Feldnamen pruefen (siehe TODO in get_squad).")
        return

    cache = load_squad_cache()
    cache[manager_id] = spieler
    save_squad_cache(cache)
    print(f"{len(spieler)} Spieler fuer Manager {manager_id} geladen und gecached.")


def login_and_get_all_squads() -> None:
    """Loggt sich einmal ein und holt die Kader ALLER Manager der Liga auf einmal."""
    token = kt.login(kt.KICKBASE_EMAIL, kt.KICKBASE_PASSWORD)
    headers = kt.auth_headers(token)
    league_id = kt.get_league_id(headers)
    managers = kt.get_managers(league_id, headers)  # liefert manager_id, name, teamwert (Dashboard)

    cache = load_squad_cache()
    for m in managers:
        manager_id = m["manager_id"]
        spieler = get_squad(league_id, manager_id, headers)
        cache[manager_id] = spieler

        summe_mw = sum(s["marktwert"] for s in spieler)
        dashboard_teamwert = m["teamwert"]
        status = "OK" if abs(summe_mw - dashboard_teamwert) < 1 else "ABWEICHUNG!"
        print(f"[{m['name']}] {len(spieler)} Spieler, Summe Marktwerte = {summe_mw:,.0f} € "
              f"vs. Dashboard-Teamwert = {dashboard_teamwert:,.0f} € -> {status}")

    save_squad_cache(cache)


def list_squad(manager_id: str) -> None:
    """Zeigt den gecachten Kader eines Managers mit Index, Position, Marktwert
    und letzter Marktwert-Aenderung."""
    cache = load_squad_cache()
    spieler = cache.get(manager_id)
    if not spieler:
        print(f"Kein gecachter Kader fuer {manager_id}. Erst login_and_get_squad(manager_id) aufrufen.")
        return

    print(f"{'#':<4} {'Pos':<5} {'Spieler':<25} {'Marktwert':>15} {'Änderung':>15}")
    for i, s in enumerate(spieler):
        aenderung = s.get("mw_aenderung", 0)
        vorzeichen = "+" if aenderung > 0 else ""
        print(f"{i:<4} {s.get('position', '?'):<5} {s['name']:<25} "
              f"{s['marktwert']:>15,} {vorzeichen}{aenderung:>14,}")
    print(f"\nSumme Marktwerte (= Teamwert lt. Kader): {sum(s['marktwert'] for s in spieler):,}")


def _max_gebot(budget: float, teamwert: float) -> float:
    """Reine 33%-Regel-Formel, OHNE Login-Bonus (Momentaufnahme, kein neuer Tag)."""
    basis = teamwert + min(budget, 0)
    return budget + kt.MINUS_GRENZE * basis


def simulate(manager_id: str, entferne_indices: list[int]) -> None:
    """
    Simuliert das Entfernen (Verkauf zum Marktwert) der Spieler mit den gegebenen
    Indizes (aus list_squad) und zeigt Vorher/Nachher fuer Teamwert, Budget,
    Netto-Teamwert und Max-Gebot.
    """
    cache = load_squad_cache()
    spieler = cache.get(manager_id)
    if not spieler:
        print(f"Kein gecachter Kader fuer {manager_id}. Erst login_and_get_squad(manager_id) aufrufen.")
        return

    state = kt.load_state()
    if manager_id not in state or not state[manager_id].get("history"):
        print(f"Kein aktueller Budget/Teamwert-Stand fuer {manager_id} in state.json gefunden.")
        return

    letzter_stand = state[manager_id]["history"][-1]
    budget_vorher = letzter_stand["budget"]
    teamwert_vorher = letzter_stand["teamwert"]

    ungueltig = [i for i in entferne_indices if i < 0 or i >= len(spieler)]
    if ungueltig:
        print(f"Ungueltige Indizes: {ungueltig} (gueltiger Bereich: 0 bis {len(spieler) - 1})")
        return

    entfernte_spieler = [spieler[i] for i in entferne_indices]
    summe_mw = sum(s["marktwert"] for s in entfernte_spieler)

    teamwert_nachher = teamwert_vorher - summe_mw
    budget_nachher = budget_vorher + summe_mw

    max_gebot_vorher = _max_gebot(budget_vorher, teamwert_vorher)
    max_gebot_nachher = _max_gebot(budget_nachher, teamwert_nachher)

    print("Entfernte Spieler (simulierter Verkauf zum Marktwert):")
    for s in entfernte_spieler:
        print(f"  - {s['name']}: {s['marktwert']:,} €")
    print(f"Summe: {summe_mw:,} €\n")

    print(f"{'':<18} {'Vorher':>16} {'Nachher':>16}")
    print(f"{'Teamwert':<18} {teamwert_vorher:>16,.0f} {teamwert_nachher:>16,.0f}")
    print(f"{'Budget':<18} {budget_vorher:>16,.0f} {budget_nachher:>16,.0f}")
    print(f"{'Netto-Teamwert':<18} {teamwert_vorher + budget_vorher:>16,.0f} "
          f"{teamwert_nachher + budget_nachher:>16,.0f}")
    print(f"{'Max-Gebot':<18} {max_gebot_vorher:>16,.0f} {max_gebot_nachher:>16,.0f}")


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Nutzung:")
        print("  python squad_simulator.py load_all")
        print("  python squad_simulator.py load <manager_id>")
        print("  python squad_simulator.py list <manager_id>")
        print("  python squad_simulator.py simulate <manager_id> <index1,index2,...>")
        sys.exit(0)

    befehl = sys.argv[1]
    if befehl == "load_all" and len(sys.argv) == 2:
        login_and_get_all_squads()
    elif befehl == "load" and len(sys.argv) == 3:
        login_and_get_squad(sys.argv[2])
    elif befehl == "list" and len(sys.argv) == 3:
        list_squad(sys.argv[2])
    elif befehl == "simulate" and len(sys.argv) == 4:
        indices = [int(x) for x in sys.argv[3].split(",")]
        simulate(sys.argv[2], indices)
    else:
        print("Unbekannter Befehl oder falsche Anzahl Argumente. Ohne Argumente aufrufen fuer Hilfe.")
