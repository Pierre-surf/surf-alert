#!/usr/bin/env python3
"""
Backtest Surf Alert Normandie
=============================

Rejoue la logique de surf_check.py sur une saison passée, en utilisant les
API HISTORIQUES d'Open-Meteo (archives), pour estimer combien de fois le
mail de validation du jeudi (+ les vérifications Ascension / Pentecôte) se
serait réellement déclenché.

Ce script est un outil de diagnostic ponctuel : il ne modifie rien à la
production, n'utilise aucun des secrets d'e-mail, et n'envoie jamais de
mail. Il se contente de compter et d'afficher les résultats.

Il réutilise directement, par import, les spots, les critères et les
fonctions de calcul de surf_check.py : si tu modifies les spots ou les
critères dans ce fichier plus tard, ce backtest suit automatiquement sans
rien à retoucher ici.

Usage :
    python backtest_surf_check.py                  # rejoue l'année précédente
    python backtest_surf_check.py --year 2025       # rejoue une année précise
    python backtest_surf_check.py --start 2024-06-01 --end 2024-09-15

Limites connues :
- Les archives Open-Meteo ont un léger délai (quelques jours) avant que les
  données les plus récentes soient disponibles : impossible de rejouer la
  saison en cours jusqu'à aujourd'hui, seulement jusqu'à J-2/J-3 environ.
- Si une donnée est manquante pour un spot/une heure donnée (trou dans
  l'archive), ce créneau est simplement ignoré pour ce spot, comme le fait
  déjà evaluate_spot_for_date() en mode normal.
"""

import argparse
import sys
from datetime import date, timedelta

import requests

# Réutilise directement la config et la logique de production : une seule
# source de vérité pour les spots, les critères et les calculs de dates.
from surf_check import (
    SPOTS,
    SEASON_START,
    SEASON_END,
    is_in_season,
    compute_target_dates,
    ascension_date,
    pentecost_monday_date,
    evaluate_spot_for_date,
)

MARINE_ARCHIVE_URL = "https://marine-api.open-meteo.com/v1/marine"
WEATHER_ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
REQUEST_TIMEOUT_S = 30
DATA_AVAILABILITY_LAG_DAYS = 3  # marge de sécurité avant "aujourd'hui"


# ---------------------------------------------------------------------------
# 1. Construction de la liste des vérifications à rejouer
# ---------------------------------------------------------------------------
def build_runs(start_date: date, end_date: date) -> list:
    """Reconstitue, pour la période demandée, la liste des vérifications
    qui auraient eu lieu en production : un run par jeudi (avec ses cibles
    week-end + jours fixes), plus un run pour Ascension et un pour
    Pentecôte si leur avant-veille tombe dans la période."""
    runs = []

    d = start_date
    while d.weekday() != 3:  # 3 = jeudi
        d += timedelta(days=1)
    while d <= end_date:
        if is_in_season(d):
            runs.append({
                "day": d,
                "trigger": "Jeudi (hebdomadaire)",
                "targets": compute_target_dates(d),
            })
        d += timedelta(days=7)

    for year in range(start_date.year, end_date.year + 1):
        for label, holiday_fn in (
            ("Ascension", ascension_date),
            ("Lundi de Pentecôte", pentecost_monday_date),
        ):
            holiday = holiday_fn(year)
            check_day = holiday - timedelta(days=2)
            if start_date <= check_day <= end_date and is_in_season(check_day):
                runs.append({
                    "day": check_day,
                    "trigger": label,
                    "targets": [{
                        "label": f"{label} ({holiday.strftime('%d/%m')})",
                        "date": holiday,
                    }],
                })

    runs.sort(key=lambda r: r["day"])
    return runs


# ---------------------------------------------------------------------------
# 2. Récupération des séries historiques (une fois par spot pour toute la
#    période, plutôt qu'un appel par run : beaucoup plus économe en requêtes)
# ---------------------------------------------------------------------------
def fetch_marine_history(lat: float, lon: float, start_date: date, end_date: date) -> dict:
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": ",".join([
            "swell_wave_height",
            "swell_wave_period",
            "secondary_swell_wave_height",
            "sea_surface_temperature",
        ]),
        "timezone": "UTC",
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
    }
    resp = requests.get(MARINE_ARCHIVE_URL, params=params, timeout=REQUEST_TIMEOUT_S)
    resp.raise_for_status()
    return resp.json()["hourly"]


def fetch_wind_history(lat: float, lon: float, start_date: date, end_date: date) -> dict:
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": "wind_speed_10m,wind_direction_10m",
        "wind_speed_unit": "kn",
        "timezone": "UTC",
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
    }
    resp = requests.get(WEATHER_ARCHIVE_URL, params=params, timeout=REQUEST_TIMEOUT_S)
    resp.raise_for_status()
    return resp.json()["hourly"]


# ---------------------------------------------------------------------------
# 3. Boucle principale
# ---------------------------------------------------------------------------
def run_backtest(start_date: date, end_date: date) -> int:
    runs = build_runs(start_date, end_date)
    if not runs:
        print("Aucune vérification à rejouer sur cette période (hors saison ?).")
        return 0

    all_target_dates = [t["date"] for r in runs for t in r["targets"]]
    fetch_start = min(start_date, min(all_target_dates))
    fetch_end = max(end_date, max(all_target_dates))

    today = date.today()
    latest_available = today - timedelta(days=DATA_AVAILABILITY_LAG_DAYS)
    if fetch_end > latest_available:
        fetch_end = latest_available
    if fetch_end < fetch_start:
        print(f"Erreur : la période demandée (jusqu'au {max(end_date, max(all_target_dates))}) "
              f"est trop récente pour les archives Open-Meteo (disponibles jusqu'au "
              f"{latest_available} environ). Choisis une période plus ancienne.")
        return 1

    print(f"Récupération des données historiques du {fetch_start} au {fetch_end} "
          f"pour {len(SPOTS)} spots...\n")

    spot_data = {}
    for spot in SPOTS:
        try:
            marine_series = fetch_marine_history(spot["lat"], spot["lon"], fetch_start, fetch_end)
            wind_series = fetch_wind_history(spot["lat"], spot["lon"], fetch_start, fetch_end)
            spot_data[spot["name"]] = (marine_series, wind_series)
            print(f"  [OK] Données récupérées pour {spot['name']}")
        except (requests.RequestException, KeyError) as exc:
            print(f"  [!] {spot['name']} : échec de récupération ({exc}) — spot ignoré pour ce backtest")

    if not spot_data:
        print("\nAucune donnée récupérée pour aucun spot. Impossible de continuer.")
        return 1

    print("\n" + "=" * 70)
    print("DÉTAIL DES VÉRIFICATIONS REJOUÉES")
    print("=" * 70)

    triggered_runs = []
    for run in runs:
        good_results = []
        for spot in SPOTS:
            if spot["name"] not in spot_data:
                continue
            marine_series, wind_series = spot_data[spot["name"]]
            for target in run["targets"]:
                result = evaluate_spot_for_date(spot, target["date"], marine_series, wind_series)
                if result:
                    result["target_label"] = target["label"]
                    good_results.append(result)

        label_targets = ", ".join(t["label"] for t in run["targets"])
        if good_results:
            triggered_runs.append(run)
            spots_hit = ", ".join(sorted(set(r["spot"] for r in good_results)))
            print(f"[MAIL ENVOYÉ] {run['day']} ({run['trigger']}) — cibles : {label_targets} "
                  f"— spots au vert : {spots_hit}")
        else:
            print(f"[  rien   ] {run['day']} ({run['trigger']}) — cibles : {label_targets}")

    # -----------------------------------------------------------------
    # 4. Résumé
    # -----------------------------------------------------------------
    nb_years = end_date.year - start_date.year + 1
    weekly_runs = [r for r in runs if r["trigger"] == "Jeudi (hebdomadaire)"]
    weekly_triggered = [r for r in triggered_runs if r["trigger"] == "Jeudi (hebdomadaire)"]
    holiday_runs = [r for r in runs if r["trigger"] != "Jeudi (hebdomadaire)"]
    holiday_triggered = [r for r in triggered_runs if r["trigger"] != "Jeudi (hebdomadaire)"]

    print("\n" + "=" * 70)
    print("RÉSUMÉ")
    print("=" * 70)
    print(f"Période rejouée      : {start_date} -> {end_date} ({nb_years} an(s))")
    print(f"Spots pris en compte : {len(spot_data)} / {len(SPOTS)}")
    print(f"Vérifications du jeudi : {len(weekly_triggered)} mail(s) envoyé(s) "
          f"sur {len(weekly_runs)} jeudis vérifiés")
    print(f"Vérifications Ascension/Pentecôte : {len(holiday_triggered)} mail(s) envoyé(s) "
          f"sur {len(holiday_runs)} vérification(s)")
    print(f"TOTAL : {len(triggered_runs)} mail(s) sur {len(runs)} vérification(s), "
          f"soit environ {len(triggered_runs) / nb_years:.1f} mail(s) par an sur la période rejouée.")

    return 0


# ---------------------------------------------------------------------------
# 5. Point d'entrée
# ---------------------------------------------------------------------------
def parse_args():
    parser = argparse.ArgumentParser(description="Backtest Surf Alert Normandie sur une saison passée.")
    parser.add_argument("--year", type=int, help="Rejoue la saison complète (15 mai -> 30 octobre) de cette année.")
    parser.add_argument("--start", type=str, help="Date de début (YYYY-MM-DD). Ignoré si --year est fourni.")
    parser.add_argument("--end", type=str, help="Date de fin (YYYY-MM-DD). Ignoré si --year est fourni.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if args.year:
        start_date = date(args.year, *SEASON_START)
        end_date = date(args.year, *SEASON_END)
    elif args.start and args.end:
        start_date = date.fromisoformat(args.start)
        end_date = date.fromisoformat(args.end)
    else:
        # Par défaut : la saison complète de l'année précédente (garantie
        # terminée, donc pas de souci de disponibilité des données).
        last_year = date.today().year - 1
        start_date = date(last_year, *SEASON_START)
        end_date = date(last_year, *SEASON_END)

    print(f"Backtest Surf Alert Normandie — période : {start_date} -> {end_date}\n")
    return run_backtest(start_date, end_date)


if __name__ == "__main__":
    sys.exit(main())
